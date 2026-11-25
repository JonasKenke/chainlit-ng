import asyncio
import socket
import threading
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web


@pytest.fixture(autouse=True)
def clear_url_fetch_environment(monkeypatch):
    for name in (
        "CHAINLIT_URL_FETCH_ALLOW_PRIVATE",
        "CHAINLIT_URL_FETCH_ALLOW_USERINFO",
        "CHAINLIT_URL_FETCH_TIMEOUT",
        "CHAINLIT_URL_FETCH_MAX_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def url_fetch():
    import chainlit.url_fetch

    return chainlit.url_fetch


@asynccontextmanager
async def local_server(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    site = web.SockSite(runner, listener)
    await site.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        await runner.cleanup()
        listener.close()


def dns_result(address, port):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


def public_dns(monkeypatch):
    original = socket.getaddrinfo
    resolved_threads = []

    def resolve(host, port, *args, **kwargs):
        resolved_threads.append(threading.get_ident())
        if host in {"public.test", "other.test"}:
            return dns_result("93.184.216.34", port)
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return resolved_threads


class FakeContent:
    def __init__(self, chunks=()):
        self.chunks = chunks

    async def iter_chunked(self, chunk_size):
        assert chunk_size > 0
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks)
        self.content_length = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/secret",
        "http://localhost/secret",
        "http://10.0.0.1/secret",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/secret",
        "http://[::ffff:127.0.0.1]/secret",
    ],
)
async def test_default_policy_denies_internal_addresses_before_request(
    url_fetch, monkeypatch, url
):
    get = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchPolicyError):
        await url_fetch.fetch_url_bytes(url)

    get.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/file",
        "http:///missing-host",
        "not-a-url",
    ],
)
async def test_default_policy_rejects_unsupported_or_missing_urls(
    url_fetch, monkeypatch, url
):
    get = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchPolicyError):
        await url_fetch.fetch_url_bytes(url)

    get.assert_not_awaited()


@pytest.mark.parametrize(
    "address",
    [
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "ff02::1",
        "fe80::1",
        "::ffff:10.0.0.1",
    ],
)
async def test_policy_rejects_special_and_mapped_addresses(
    url_fetch, monkeypatch, address
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: dns_result(address, port),
    )
    get = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchPolicyError):
        await url_fetch.fetch_url_bytes("http://public.test/resource")

    get.assert_not_awaited()


async def test_policy_rejects_mixed_public_and_private_dns_answers(
    url_fetch, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: (
            dns_result("93.184.216.34", port) + dns_result("127.0.0.1", port)
        ),
    )
    get = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchPolicyError):
        await url_fetch.fetch_url_bytes("http://public.test/resource")

    get.assert_not_awaited()


async def test_private_opt_in_allows_local_server_and_resolves_off_event_loop(
    url_fetch, monkeypatch
):
    requests = []

    async def handler(request):
        requests.append(request.path)
        return web.Response(body=b"local-content")

    resolved_threads = public_dns(monkeypatch)
    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")

    async with local_server(handler) as base_url:
        result = await url_fetch.fetch_url_bytes(
            f"{base_url.replace('127.0.0.1', 'localhost')}/file"
        )

    assert result == b"local-content"
    assert requests == ["/file"]
    assert resolved_threads
    assert all(thread != threading.get_ident() for thread in resolved_threads)


async def test_url_userinfo_is_rejected_by_default(url_fetch, monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchPolicyError):
        await url_fetch.fetch_url_bytes("http://user:password@public.test/file")

    get.assert_not_awaited()


async def test_userinfo_opt_in_supports_legacy_auth_without_url_credentials(
    url_fetch, monkeypatch
):
    public_dns(monkeypatch)
    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_USERINFO", "true")
    get = AsyncMock(return_value=FakeResponse(chunks=[b"authenticated"]))
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    result = await url_fetch.fetch_url_bytes("http://user:password@public.test/file")

    assert result == b"authenticated"
    called_url = get.await_args.args[0]
    assert called_url == "http://public.test/file"
    assert get.await_args.kwargs["headers"] == {
        "Authorization": "Basic dXNlcjpwYXNzd29yZA=="
    }


async def test_redirect_to_private_address_is_blocked_before_target_request(
    url_fetch, monkeypatch
):
    public_dns(monkeypatch)
    target_requests = []

    async def handler(request):
        target_requests.append(request.path)
        return web.Response(body=b"secret")

    get = AsyncMock(
        return_value=FakeResponse(
            status=302,
            headers={"Location": "http://127.0.0.1/secret"},
        )
    )
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    async with local_server(handler) as base_url:
        redirect = FakeResponse(
            status=302,
            headers={"Location": f"{base_url}/secret"},
        )
        get.return_value = redirect
        with pytest.raises(url_fetch.URLFetchPolicyError):
            await url_fetch.fetch_url_bytes("http://public.test/start")

    assert get.await_count == 1
    assert target_requests == []


async def test_same_origin_redirect_is_allowed(url_fetch, monkeypatch):
    public_dns(monkeypatch)
    get = AsyncMock(
        side_effect=[
            FakeResponse(status=302, headers={"Location": "/final"}),
            FakeResponse(chunks=[b"redirected"]),
        ]
    )
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    result = await url_fetch.fetch_url_bytes("http://public.test/start")

    assert result == b"redirected"
    assert [call.args[0] for call in get.await_args_list] == [
        "http://public.test/start",
        "http://public.test/final",
    ]


async def test_redirect_limit_allows_three_hops(url_fetch, monkeypatch):
    public_dns(monkeypatch)
    get = AsyncMock(
        side_effect=[
            FakeResponse(status=302, headers={"Location": "/hop-1"}),
            FakeResponse(status=302, headers={"Location": "/hop-2"}),
            FakeResponse(status=302, headers={"Location": "/hop-3"}),
            FakeResponse(chunks=[b"done"]),
        ]
    )
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    assert await url_fetch.fetch_url_bytes("http://public.test/start") == b"done"
    assert get.await_count == 4


async def test_fourth_redirect_is_rejected(url_fetch, monkeypatch):
    public_dns(monkeypatch)
    get = AsyncMock(
        return_value=FakeResponse(status=302, headers={"Location": "/again"})
    )
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchError, match="redirect"):
        await url_fetch.fetch_url_bytes("http://public.test/start")

    assert get.await_count == 4


async def test_cross_origin_redirect_drops_auth_and_target_credentials(
    url_fetch, monkeypatch
):
    public_dns(monkeypatch)
    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_USERINFO", "true")
    get = AsyncMock(
        side_effect=[
            FakeResponse(
                status=302,
                headers={"Location": "http://other:password@other.test/final"},
            ),
            FakeResponse(chunks=[b"done"]),
        ]
    )
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    assert (
        await url_fetch.fetch_url_bytes("http://user:password@public.test/start")
        == b"done"
    )
    assert get.await_args_list[0].kwargs["headers"] == {
        "Authorization": "Basic dXNlcjpwYXNzd29yZA=="
    }
    assert get.await_args_list[1].args[0] == "http://other.test/final"
    assert get.await_args_list[1].kwargs["headers"] is None


async def test_oversized_chunked_body_is_rejected_without_response_read(
    url_fetch, monkeypatch
):
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for _ in range(16):
                await response.write(b"x" * 1024)
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    def forbidden_read(*args, **kwargs):
        raise AssertionError("fetch_url_bytes must not call response.read()")

    monkeypatch.setattr(aiohttp.ClientResponse, "read", forbidden_read)
    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")

    async with local_server(handler) as base_url:
        with pytest.raises(url_fetch.URLFetchTooLargeError):
            await url_fetch.fetch_url_bytes(f"{base_url}/large", max_bytes=1024)


async def test_declared_oversized_body_is_rejected_before_streaming(
    url_fetch, monkeypatch
):
    async def handler(request):
        return web.Response(body=b"0123456789")

    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")

    async with local_server(handler) as base_url:
        with pytest.raises(url_fetch.URLFetchTooLargeError):
            await url_fetch.fetch_url_bytes(f"{base_url}/large", max_bytes=9)


async def test_exact_byte_limit_is_accepted(url_fetch, monkeypatch):
    async def handler(request):
        return web.Response(body=b"0123456789")

    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")

    async with local_server(handler) as base_url:
        assert (
            await url_fetch.fetch_url_bytes(f"{base_url}/file", max_bytes=10)
            == b"0123456789"
        )


async def test_max_bytes_environment_override_is_enforced(url_fetch, monkeypatch):
    async def handler(request):
        return web.Response(body=b"0123456789")

    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("CHAINLIT_URL_FETCH_MAX_BYTES", "9")

    async with local_server(handler) as base_url:
        with pytest.raises(url_fetch.URLFetchTooLargeError):
            await url_fetch.fetch_url_bytes(f"{base_url}/file")


async def test_timeout_environment_override_terminates_slow_response(
    url_fetch, monkeypatch
):
    async def handler(request):
        await asyncio.sleep(0.2)
        return web.Response(body=b"late")

    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("CHAINLIT_URL_FETCH_TIMEOUT", "0.05")

    async with local_server(handler) as base_url:
        with pytest.raises(url_fetch.URLFetchTimeoutError):
            await url_fetch.fetch_url_bytes(f"{base_url}/slow")


async def test_total_timeout_terminates_slow_drip(url_fetch, monkeypatch):
    async def handler(request):
        response = web.StreamResponse()
        await response.prepare(request)
        try:
            for _ in range(4):
                await response.write(b"x")
                await asyncio.sleep(0.04)
            await response.write_eof()
        except ConnectionResetError:
            pass
        return response

    monkeypatch.setenv("CHAINLIT_URL_FETCH_ALLOW_PRIVATE", "true")

    async with local_server(handler) as base_url:
        with pytest.raises(url_fetch.URLFetchTimeoutError):
            await url_fetch.fetch_url_bytes(f"{base_url}/drip", timeout=0.1)


async def test_http_error_does_not_expose_response_body(url_fetch, monkeypatch):
    public_dns(monkeypatch)
    secret = "response-body-secret"
    get = AsyncMock(return_value=FakeResponse(status=500, chunks=[secret.encode()]))
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)

    with pytest.raises(url_fetch.URLFetchHTTPError) as exc_info:
        await url_fetch.fetch_url_bytes("http://public.test/failure")

    assert exc_info.value.status == 500
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHAINLIT_URL_FETCH_TIMEOUT", "0"),
        ("CHAINLIT_URL_FETCH_TIMEOUT", "nan"),
        ("CHAINLIT_URL_FETCH_MAX_BYTES", "-1"),
        ("CHAINLIT_URL_FETCH_MAX_BYTES", "invalid"),
    ],
)
async def test_invalid_environment_limits_fail_deterministically(
    url_fetch, monkeypatch, name, value
):
    get = AsyncMock()
    monkeypatch.setattr(aiohttp.ClientSession, "get", get)
    monkeypatch.setenv(name, value)

    with pytest.raises(url_fetch.URLFetchConfigurationError):
        await url_fetch.fetch_url_bytes("http://public.test/file")

    get.assert_not_awaited()


async def test_sqlalchemy_create_element_uses_shared_fetcher(
    monkeypatch, mock_chainlit_context
):
    from chainlit.data import sql_alchemy
    from chainlit.element import File

    layer = object.__new__(sql_alchemy.SQLAlchemyDataLayer)
    layer.show_logger = False
    layer._get_user_id_by_thread = AsyncMock(return_value="user-1")
    layer.execute_sql = AsyncMock()
    storage = AsyncMock()
    storage.upload_file.return_value = {
        "url": "https://storage.test/file",
        "object_key": "user-1/element-1/file",
    }
    layer.storage_provider = storage
    fetch = AsyncMock(return_value=b"remote-element")
    monkeypatch.setattr(sql_alchemy, "fetch_url_bytes", fetch, raising=False)
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "get",
        AsyncMock(side_effect=AssertionError("legacy HTTP client was used")),
    )
    element = File(
        id="element-1",
        thread_id="thread-1",
        for_id="step-1",
        name="remote.txt",
        url="https://public.test/remote.txt",
    )

    async with mock_chainlit_context:
        await layer.create_element(element)

    fetch.assert_awaited_once_with(element.url)
    assert storage.upload_file.await_args.kwargs["data"] == b"remote-element"
    layer.execute_sql.assert_awaited_once()


async def test_dynamodb_create_element_uses_shared_fetcher(
    monkeypatch, mock_chainlit_context
):
    from chainlit.data.dynamodb import DynamoDBDataLayer
    from chainlit.element import File

    storage = AsyncMock()
    storage.upload_file.return_value = {
        "url": "https://storage.test/file",
        "object_key": "user-1/thread-1/element-1",
    }
    layer = DynamoDBDataLayer(
        table_name="elements", client=Mock(), storage_provider=storage
    )
    fetch = AsyncMock(return_value=b"remote-element")
    monkeypatch.setattr("chainlit.data.dynamodb.fetch_url_bytes", fetch, raising=False)
    monkeypatch.setattr(
        aiohttp.ClientSession,
        "get",
        AsyncMock(side_effect=AssertionError("legacy HTTP client was used")),
    )
    element = File(
        id="element-1",
        thread_id="thread-1",
        for_id="step-1",
        name="remote.txt",
        url="https://public.test/remote.txt",
    )

    async with mock_chainlit_context:
        await layer.create_element(element)

    fetch.assert_awaited_once_with(element.url)
    assert storage.upload_file.await_args.kwargs["data"] == b"remote-element"
    layer.client.put_item.assert_called_once()


async def test_langflow_load_flow_uses_shared_fetcher(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType

    import httpx

    import chainlit.utils

    langflow = ModuleType("langflow")
    load_flow_from_json = Mock(return_value=object())
    langflow.load_flow_from_json = load_flow_from_json
    monkeypatch.setitem(sys.modules, "langflow", langflow)
    monkeypatch.setattr(chainlit.utils, "check_module_version", lambda *args: True)
    monkeypatch.setattr(
        httpx.AsyncClient,
        "get",
        AsyncMock(side_effect=AssertionError("legacy HTTP client was used")),
    )
    path = Path(__file__).parents[1] / "chainlit" / "langflow" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_langflow_url_fetch_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fetch = AsyncMock(return_value=b'{"nodes": []}')
    monkeypatch.setattr(module, "fetch_url_bytes", fetch, raising=False)

    result = await module.load_flow("https://public.test/flow.json", tweaks={"x": 1})

    assert result is load_flow_from_json.return_value
    fetch.assert_awaited_once_with("https://public.test/flow.json")
    load_flow_from_json.assert_called_once_with(flow={"nodes": []}, tweaks={"x": 1})
