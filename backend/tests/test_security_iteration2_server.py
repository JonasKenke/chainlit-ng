import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import chainlit.server as server
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.server import (
    _login_attempt_key,
    _mcp_capacity_reached,
    _socket_origin_allowed,
    app,
)
from chainlit.session import WebsocketSession
from chainlit.user import User

MCP_URL = "https://mcp.example.com/mcp"
SECRET = "iteration2-test-secret-with-at-least-32-bytes"


def login_request(host="203.0.113.10"):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "scheme": "https",
            "server": ("testserver", 443),
            "client": (host, 12345),
            "root_path": "",
        }
    )


def test_login_key_hashes_complete_username_and_host():
    prefix = "a" * 200
    first = _login_attempt_key(login_request(), prefix + "first")
    second = _login_attempt_key(login_request(), prefix + "second")

    assert first != second
    assert first == _login_attempt_key(login_request(), (prefix + "FIRST").upper())
    assert first == _login_attempt_key(login_request(), prefix + "first")
    assert first != _login_attempt_key(login_request("203.0.113.11"), prefix + "first")
    assert len(first) < 100


@pytest.mark.parametrize(
    ("origin", "environ", "expected"),
    [
        (
            "http://testserver",
            {"HTTP_HOST": "testserver", "wsgi.url_scheme": "https"},
            False,
        ),
        (
            "https://testserver",
            {"HTTP_HOST": "testserver", "wsgi.url_scheme": "https"},
            True,
        ),
        (
            "http://testserver",
            {"HTTP_HOST": "testserver", "wsgi.url_scheme": "http"},
            True,
        ),
        (
            "https://testserver",
            {
                "HTTP_HOST": "testserver",
                "wsgi.url_scheme": "http",
                "HTTP_X_FORWARDED_PROTO": "https, http",
            },
            True,
        ),
        (
            "http://testserver",
            {
                "HTTP_HOST": "testserver",
                "wsgi.url_scheme": "http",
                "HTTP_X_FORWARDED_PROTO": "https, http",
            },
            False,
        ),
    ],
)
def test_socket_origin_requires_effective_scheme(
    origin, environ, expected, monkeypatch
):
    monkeypatch.setattr(config.project, "allow_origins", ["https://testserver"])
    monkeypatch.setattr(server, "require_login", lambda: True)
    assert _socket_origin_allowed(origin, environ) is expected


def test_socket_origin_chainlit_url_and_explicit_allowlist_are_scheme_aware(
    monkeypatch,
):
    monkeypatch.setenv("CHAINLIT_URL", "https://testserver")
    monkeypatch.setattr(config.project, "allow_origins", ["https://testserver"])
    monkeypatch.setattr(server, "require_login", lambda: True)
    assert (
        _socket_origin_allowed(
            "http://testserver", {"HTTP_HOST": "testserver", "wsgi.url_scheme": "https"}
        )
        is False
    )

    monkeypatch.delenv("CHAINLIT_URL")
    monkeypatch.setattr(config.project, "allow_origins", ["https://testserver"])
    assert (
        _socket_origin_allowed(
            "http://testserver", {"HTTP_HOST": "testserver", "wsgi.url_scheme": "https"}
        )
        is False
    )


def test_engineio_https_handshake_rejects_http_origin(monkeypatch):
    monkeypatch.setenv("CHAINLIT_CUSTOM_AUTH", "1")
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    monkeypatch.setattr(config.project, "allow_origins", ["*"])
    token = jwt.encode(
        {"identifier": "alice", "metadata": {}, "exp": int(time.time()) + 300},
        SECRET,
        algorithm="HS256",
    )
    client = TestClient(app, base_url="https://testserver")
    path = "/ws/socket.io/?EIO=4&transport=polling"
    headers = {"Cookie": f"access_token={token}"}

    https = client.get(path, headers={**headers, "Origin": "https://testserver"})
    http = client.get(path, headers={**headers, "Origin": "http://testserver"})

    assert https.status_code == 200
    assert http.status_code == 400


@pytest.mark.parametrize("path", ["/user", "/project/settings"])
def test_user_settings_responses_are_private_no_store(path):
    app.dependency_overrides[get_current_user] = lambda: User(identifier="alice")
    try:
        response = TestClient(app).get(path)
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


class FakeAsyncClient:
    instances: list[Any] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True
        return False

    async def aclose(self):
        self.closed = True


class FakeClientSession:
    initialized = 0
    gate = None
    failure = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def discover(self):
        raise RuntimeError("legacy transport")

    async def initialize(self):
        type(self).initialized += 1
        if type(self).failure is not None:
            raise type(self).failure("connect failed")
        if type(self).gate is not None:
            await type(self).gate.wait()

    async def list_tools(self, *, params=None):
        return SimpleNamespace(tools=[SimpleNamespace(name="echo")], next_cursor=None)

    async def list_prompts(self, *, params=None):
        return SimpleNamespace(prompts=[], next_cursor=None)

    async def list_resources(self, *, params=None):
        return SimpleNamespace(resources=[], next_cursor=None)


@asynccontextmanager
async def fake_streamable(*_args, **_kwargs):
    yield AsyncMock(), AsyncMock()


async def fake_describe(_client, _name):
    return {"tools": [{"name": "echo"}], "prompts": [], "resources": []}


def make_mcp_session():
    token = uuid.uuid4().hex
    return WebsocketSession(
        id=f"iteration2-mcp-{token}",
        socket_id=f"iteration2-sid-{token}",
        emit=AsyncMock(),
        emit_call=AsyncMock(),
        user_env={},
        client_type="webapp",
    )


@pytest.fixture
def mcp_transport(monkeypatch):
    FakeAsyncClient.instances = []
    FakeClientSession.initialized = 0
    FakeClientSession.gate = None
    FakeClientSession.failure = None
    monkeypatch.setattr(server, "_MAX_MCP_SESSIONS", 2)
    monkeypatch.setattr(config.features.mcp, "enabled", True)
    monkeypatch.setattr(config.features.mcp, "servers", [])
    monkeypatch.setattr(config.features.mcp.user_servers, "enabled", True)
    monkeypatch.setattr(config.features.mcp.user_servers, "allowed_urls", [MCP_URL])
    monkeypatch.setattr("httpx2.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("mcp.ClientSession", FakeClientSession)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", fake_streamable
    )
    monkeypatch.setattr(server, "describe_mcp_server", fake_describe)
    yield
    FakeClientSession.gate = None


async def close_mcp_session(session):
    for item in list(session.mcp_sessions.values()):
        await item.close()
    session.mcp_sessions.clear()
    await session.delete()


async def post_mcp(client, session, name):
    return await client.post(
        "/mcp",
        json={
            "sessionId": session.id,
            "name": name,
            "clientType": "streamable-http",
            "url": MCP_URL,
            "useOAuth": False,
        },
    )


async def wait_until(predicate, timeout=2):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_mcp_concurrent_asgi_burst_respects_cap(mcp_transport):
    session = make_mcp_session()
    gate = asyncio.Event()
    FakeClientSession.gate = gate
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            requests = [
                asyncio.create_task(post_mcp(client, session, f"concurrent-{i}"))
                for i in range(10)
            ]
            await wait_until(lambda: FakeClientSession.initialized >= 2)
            await asyncio.sleep(0.05)
            entered_before_release = FakeClientSession.initialized
            gate.set()
            responses = await asyncio.gather(*requests)

        assert entered_before_release == 2
        assert [response.status_code for response in responses].count(200) == 2
        assert [response.status_code for response in responses].count(429) == 8
        assert len(session.mcp_sessions) == 2
        assert session.mcp_reservations == {}
    finally:
        FakeClientSession.gate = None
        await close_mcp_session(session)


@pytest.mark.asyncio
async def test_mcp_concurrent_same_name_start_is_rejected(mcp_transport):
    session = make_mcp_session()
    gate = asyncio.Event()
    FakeClientSession.gate = gate
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            requests = [
                asyncio.create_task(post_mcp(client, session, "same-name"))
                for _ in range(10)
            ]
            await wait_until(lambda: FakeClientSession.initialized == 1)
            await asyncio.sleep(0.05)
            gate.set()
            responses = await asyncio.gather(*requests)

        assert FakeClientSession.initialized == 1
        assert [response.status_code for response in responses].count(200) == 1
        assert [response.status_code for response in responses].count(429) == 9
        assert len(session.mcp_sessions) == 1
        assert session.mcp_reservations == {}
    finally:
        FakeClientSession.gate = None
        await close_mcp_session(session)


@pytest.mark.asyncio
async def test_mcp_feature_disabled_shape_is_unchanged(mcp_transport, monkeypatch):
    session = make_mcp_session()
    monkeypatch.setattr(config.features.mcp, "enabled", False)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await post_mcp(client, session, "disabled")

        assert response.status_code == 400
        assert response.json() == {"detail": "This app does not support MCP."}
        assert session.mcp_reservations == {}
        assert FakeClientSession.initialized == 0
    finally:
        await close_mcp_session(session)


@pytest.mark.asyncio
async def test_mcp_sequential_cap_and_same_name_replacement(mcp_transport):
    session = make_mcp_session()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            responses = [
                await post_mcp(client, session, f"sequential-{i}") for i in range(5)
            ]
            replacement = await post_mcp(client, session, "sequential-0")

        assert [response.status_code for response in responses] == [
            200,
            200,
            429,
            429,
            429,
        ]
        assert replacement.status_code == 200
        assert len(session.mcp_sessions) == 2
        assert session.mcp_reservations == {}
    finally:
        await close_mcp_session(session)


@pytest.mark.asyncio
async def test_mcp_failed_reservation_is_released(mcp_transport):
    session = make_mcp_session()
    FakeClientSession.failure = RuntimeError
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            failed = await post_mcp(client, session, "retry")
            assert failed.status_code == 400
            assert session.mcp_reservations == {}

            FakeClientSession.failure = None
            retry = await post_mcp(client, session, "retry")

        assert retry.status_code == 200
        assert len(session.mcp_sessions) == 1
        assert session.mcp_reservations == {}
    finally:
        await close_mcp_session(session)


@pytest.mark.asyncio
async def test_mcp_cancelled_reservation_is_released(mcp_transport):
    session = make_mcp_session()
    gate = asyncio.Event()
    FakeClientSession.gate = gate
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            request = asyncio.create_task(post_mcp(client, session, "cancelled"))
            await wait_until(lambda: FakeClientSession.initialized == 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.sleep(0.05)

        assert session.mcp_reservations == {}
        assert session.mcp_sessions == {}
    finally:
        FakeClientSession.gate = None
        await close_mcp_session(session)


def test_mcp_capacity_counts_reservations(mcp_transport):
    session = SimpleNamespace(
        mcp_sessions={},
        mcp_oauth_tasks={},
        mcp_reservations={"one": 1, "two": 1},
    )

    assert _mcp_capacity_reached(session, "three") is True
    assert _mcp_capacity_reached(session, "one") is True
