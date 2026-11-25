import asyncio
import socket
from collections.abc import Callable
from typing import Any

import anyio
import httpx2
import pytest
import pytest_asyncio

from chainlit.mcp import (
    McpDestinationError,
    _destination_in_allowlist,
    _destination_on_origin,
    make_mcp_http_client_factory,
    validate_mcp_headers,
)


class AsyncBody(httpx2.AsyncByteStream):
    async def __aiter__(self):
        yield b"ok"

    async def aclose(self) -> None:
        pass


class LocalMcpServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responder: Callable[
            [dict[str, Any]], tuple[int, bytes, dict[str, str]]
        ] = lambda _request: (200, b"ok", {})
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        assert self._server is not None
        return getattr(self._server, "sockets")[0].getsockname()[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers = {
                name.lower(): value.strip()
                for name, value in (line.split(":", 1) for line in lines[1:] if line)
            }
            request = {"method": method, "target": target, "headers": headers}
            self.requests.append(request)
            status, body, response_headers = self.responder(request)
            reason = "OK" if status == 200 else "Found"
            response_headers = {"Connection": "close", **response_headers}
            head = f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
            head += b"".join(
                f"{name}: {value}\r\n".encode("latin-1")
                for name, value in response_headers.items()
            )
            head += f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            writer.write(head + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


@pytest_asyncio.fixture
async def local_server():
    server = LocalMcpServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch):
    answers: dict[str, list[str]] = {}
    calls: list[tuple[str, int, dict[str, Any]]] = []

    async def getaddrinfo(host, port, **kwargs):
        calls.append((host, port, kwargs))
        if host not in answers:
            raise socket.gaierror(f"no mock answer for {host}")
        result = []
        for address in answers[host]:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (
                (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            )
            result.append(
                (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)
            )
        return result

    monkeypatch.setattr(anyio, "getaddrinfo", getaddrinfo)
    return answers, calls


@pytest.fixture
def local_network(monkeypatch: pytest.MonkeyPatch, local_server: LocalMcpServer):
    connect_tcp = anyio.connect_tcp
    hosts: list[str] = []

    async def connect_locally(remote_host, remote_port, local_host=None):
        hosts.append(remote_host)
        return await connect_tcp(
            remote_host="127.0.0.1",
            remote_port=local_server.port,
            local_host=local_host,
        )

    monkeypatch.setattr(anyio, "connect_tcp", connect_locally)
    return hosts


def make_client(check, *, headers=None, on_blocked=None):
    return make_mcp_http_client_factory(check, on_blocked=on_blocked)(headers=headers)


async def test_public_destination_is_accepted_and_address_is_pinned(
    dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    headers = validate_mcp_headers(
        {
            "Authorization": "Bearer user-token",
            "Cookie": "session=secret",
            "X_Forwarded_For": "203.0.113.7",
        }
    )
    client = make_client(
        _destination_in_allowlist(["http://mcp.example.com/mcp"]), headers=headers
    )
    try:
        response = await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert local_network == ["93.184.216.34"]
    assert calls[0][0] == "mcp.example.com"
    assert calls[0][2]["family"] == socket.AF_UNSPEC
    assert calls[0][2]["type"] == socket.SOCK_STREAM
    sent = local_server.requests[0]["headers"]
    assert sent["host"] == "mcp.example.com"
    assert sent["authorization"] == "Bearer user-token"
    assert "cookie" not in sent
    assert "x-forwarded-for" not in sent


async def test_public_ipv6_destination_is_accepted_and_pinned(
    dns, local_network, local_server: LocalMcpServer
):
    answers, _calls = dns
    answers["mcp.example.com"] = ["2606:4700:4700::1111"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        response = await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert local_network == ["2606:4700:4700::1111"]
    assert local_server.requests[0]["headers"]["host"] == "mcp.example.com"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "169.254.169.254",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "100.64.0.1",
        "192.0.2.1",
        "::1",
        "fe80::1",
        "ff02::1",
        "::",
        "::ffff:127.0.0.1",
    ],
)
async def test_non_public_destination_is_denied_by_default(
    address, dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = [address]
    blocked: list[McpDestinationError] = []
    client = make_client(
        _destination_in_allowlist(["http://mcp.example.com/mcp"]),
        on_blocked=blocked.append,
    )
    try:
        with pytest.raises(McpDestinationError, match="non-public address"):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert len(calls) == 1
    assert local_network == []
    assert local_server.requests == []
    assert len(blocked) == 1
    assert "CHAINLIT_MCP_ALLOW_PRIVATE=true" in str(blocked[0])


async def test_mixed_public_and_private_answers_are_denied(
    dns, local_network, local_server: LocalMcpServer
):
    answers, _calls = dns
    answers["mcp.example.com"] = ["93.184.216.34", "127.0.0.1"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        with pytest.raises(McpDestinationError, match="non-public address"):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert local_network == []
    assert local_server.requests == []


async def test_private_opt_in_allows_local_mock(
    monkeypatch: pytest.MonkeyPatch, dns, local_network, local_server: LocalMcpServer
):
    monkeypatch.setenv("CHAINLIT_MCP_ALLOW_PRIVATE", "true")
    answers, calls = dns
    answers["mcp.example.com"] = ["127.0.0.1"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        response = await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert len(calls) == 1
    assert len(local_server.requests) == 1


@pytest.mark.parametrize("value", ["false", "1", "yes", ""])
async def test_private_opt_in_requires_explicit_true(
    value,
    monkeypatch: pytest.MonkeyPatch,
    dns,
    local_network,
    local_server: LocalMcpServer,
):
    monkeypatch.setenv("CHAINLIT_MCP_ALLOW_PRIVATE", value)
    answers, _calls = dns
    answers["mcp.example.com"] = ["127.0.0.1"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        with pytest.raises(McpDestinationError):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert local_network == []
    assert local_server.requests == []


async def test_every_request_revalidates_dns_and_blocks_private_rebinding(
    dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        first = await client.get("http://mcp.example.com/mcp")
        assert first.status_code == 200

        answers["mcp.example.com"] = ["127.0.0.1"]
        with pytest.raises(McpDestinationError, match="non-public address"):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert [host for host, _port, _kwargs in calls] == [
        "mcp.example.com",
        "mcp.example.com",
    ]
    assert local_network == ["93.184.216.34"]
    assert len(local_server.requests) == 1


async def test_reconnect_revalidates_dns_and_keeps_the_validated_address_pinned(
    dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        first = await client.get("http://mcp.example.com/mcp")
        assert first.status_code == 200

        answers["mcp.example.com"] = ["1.1.1.1"]
        second = await client.get("http://mcp.example.com/mcp")
        assert second.status_code == 200
    finally:
        await client.aclose()

    assert len(calls) == 2
    assert local_network == ["93.184.216.34", "93.184.216.34"]
    assert len(local_server.requests) == 2


async def test_redirects_remain_disabled(
    dns, local_network, local_server: LocalMcpServer
):
    answers, _calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    local_server.responder = lambda _request: (
        302,
        b"",
        {"Location": "/mcp/next"},
    )
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        response = await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert response.status_code == 302
    assert client.follow_redirects is False
    assert [request["target"] for request in local_server.requests] == ["/mcp"]


async def test_redirect_target_is_revalidated_when_following_is_enabled(
    dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]

    def redirect_to_rebound_host(_request):
        answers["mcp.example.com"] = ["127.0.0.1"]
        return 302, b"", {"Location": "/mcp/next"}

    local_server.responder = redirect_to_rebound_host
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    client.follow_redirects = True
    try:
        with pytest.raises(McpDestinationError, match="non-public address"):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert len(calls) == 2
    assert local_network == ["93.184.216.34"]
    assert [request["target"] for request in local_server.requests] == ["/mcp"]


async def test_redirect_to_disallowed_origin_is_blocked_before_dns(
    dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    local_server.responder = lambda _request: (
        302,
        b"",
        {"Location": "http://other.example.com/mcp"},
    )
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    client.follow_redirects = True
    try:
        with pytest.raises(
            McpDestinationError, match="not in the allowed MCP URL list"
        ):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert [host for host, _port, _kwargs in calls] == ["mcp.example.com"]
    assert len(local_network) == 1
    assert len(local_server.requests) == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp-evil",
        "http://other.example.com/mcp",
    ],
)
async def test_allowlist_and_sibling_path_controls_precede_dns(
    url, dns, local_network, local_server: LocalMcpServer
):
    answers, calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    answers["other.example.com"] = ["93.184.216.35"]
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        with pytest.raises(McpDestinationError):
            await client.get(url)
    finally:
        await client.aclose()

    assert calls == []
    assert local_network == []
    assert local_server.requests == []


async def test_named_server_sibling_path_is_allowed_after_resolution(
    dns, local_network, local_server: LocalMcpServer
):
    answers, _calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    client = make_client(_destination_on_origin("http://mcp.example.com/sse"))
    try:
        response = await client.get("http://mcp.example.com/messages/")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert local_network == ["93.184.216.34"]
    assert local_server.requests[0]["target"] == "/messages/"


async def test_dns_failure_is_blocked_before_network(
    dns, local_network, local_server: LocalMcpServer
):
    _answers, calls = dns
    blocked: list[McpDestinationError] = []
    client = make_client(
        _destination_in_allowlist(["http://mcp.example.com/mcp"]),
        on_blocked=blocked.append,
    )
    try:
        with pytest.raises(McpDestinationError, match="could not be resolved"):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert len(calls) == 1
    assert local_network == []
    assert local_server.requests == []
    assert len(blocked) == 1


async def test_empty_dns_answer_is_blocked_before_network(
    dns, local_network, local_server: LocalMcpServer
):
    answers, _calls = dns
    answers["mcp.example.com"] = []
    client = make_client(_destination_in_allowlist(["http://mcp.example.com/mcp"]))
    try:
        with pytest.raises(McpDestinationError, match="no addresses"):
            await client.get("http://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert local_network == []
    assert local_server.requests == []


async def test_https_preserves_host_and_sni_while_pinning(
    monkeypatch: pytest.MonkeyPatch, dns
):
    answers, _calls = dns
    answers["mcp.example.com"] = ["93.184.216.34"]
    sent: list[httpx2.Request] = []

    async def handle_request(_transport, request):
        sent.append(request)
        return httpx2.Response(200, stream=AsyncBody())

    monkeypatch.setattr(
        httpx2.AsyncHTTPTransport, "handle_async_request", handle_request
    )
    client = make_client(
        _destination_in_allowlist(["https://mcp.example.com/mcp"]),
        headers={"Authorization": "Bearer user-token"},
    )
    try:
        response = await client.get("https://mcp.example.com/mcp")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0].url.host == "93.184.216.34"
    assert sent[0].headers["host"] == "mcp.example.com"
    assert sent[0].headers["authorization"] == "Bearer user-token"
    assert sent[0].extensions["sni_hostname"] == "mcp.example.com"
