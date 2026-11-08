import logging
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.requests import Request

from chainlit.context import (
    ChainlitContext,
    ChainlitContextException,
    RequestContextMiddleware,
    context_var,
    get_context,
    init_http_context,
    init_ws_context,
    request,
    request_var,
)
from chainlit.emitter import BaseChainlitEmitter, ChainlitEmitter
from chainlit.session import HTTPSession, WebsocketSession


@pytest.fixture
def mock_emitter():
    return Mock(spec=BaseChainlitEmitter)


async def test_chainlit_context_init_with_websocket(
    mock_websocket_session, mock_emitter
):
    context = ChainlitContext(mock_websocket_session, mock_emitter)
    assert isinstance(context.emitter, BaseChainlitEmitter)
    assert context.session == mock_websocket_session


async def test_chainlit_context_init_with_http(mock_http_session):
    context = ChainlitContext(mock_http_session)
    assert isinstance(context.emitter, BaseChainlitEmitter)
    assert context.session == mock_http_session


async def test_init_ws_context(mock_websocket_session):
    context = init_ws_context(mock_websocket_session)
    assert isinstance(context, ChainlitContext)
    assert context.session == mock_websocket_session
    assert isinstance(context.emitter, ChainlitEmitter)


async def test_init_http_context():
    context = init_http_context()
    assert isinstance(context, ChainlitContext)
    assert isinstance(context.session, HTTPSession)
    assert isinstance(context.emitter, BaseChainlitEmitter)


async def test_get_context():
    with pytest.raises(ChainlitContextException):
        get_context()

    init_http_context()  # Initialize a context
    context = get_context()
    assert isinstance(context, ChainlitContext)


def _http_scope(path="/project/share/t1", method="GET", headers=None):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"a=1",
        "root_path": "",
        "headers": headers or [(b"host", b"chat.example.com")],
        "client": ("203.0.113.7", 54321),
        "server": ("chat.example.com", 443),
    }


def _websocket_session(scope):
    return WebsocketSession(
        id=str(uuid.uuid4()),
        socket_id=str(uuid.uuid4()),
        emit=Mock(),
        emit_call=Mock(),
        user_env={},
        client_type="webapp",
        environ={"asgi.scope": scope},
    )


async def test_request_is_none_without_a_context():
    assert request() is None


async def test_request_from_websocket_session():
    scope = _http_scope(
        path="/ws/socket.io/",
        method="POST",
        headers=[
            (b"host", b"chat.example.com"),
            (b"cookie", b"session=abc123"),
            (b"authorization", b"Bearer token"),
        ],
    )
    init_ws_context(_websocket_session(scope))

    req = request()

    assert req is not None
    assert req.method == "GET"
    assert req.url.path == "/ws/socket.io/"
    assert req.query_params["a"] == "1"
    assert req.cookies == {"session": "abc123"}
    assert req.headers["authorization"] == "Bearer token"


async def test_request_from_http_session():
    scope = _http_scope(path="/project/share/thread-1")
    session = HTTPSession(
        id="http_session",
        client_type="webapp",
        user_env={},
        environ={"asgi.scope": scope},
    )
    context_var.set(ChainlitContext(session))

    req = request()

    assert req is not None
    assert req.url.path == "/project/share/thread-1"


async def test_request_prefers_the_session_over_the_live_http_request():
    session_scope = _http_scope(path="/ws/socket.io/")
    init_ws_context(_websocket_session(session_scope))
    token = request_var.set(Request(_http_scope(path="/project/settings")))
    try:
        assert request().url.path == "/ws/socket.io/"
    finally:
        request_var.reset(token)


async def test_request_context_middleware_exposes_the_live_request():
    captured = {}

    async def inner_app(scope, receive, send):
        captured["request"] = request()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(message):
        sent.append(message)

    middleware = RequestContextMiddleware(inner_app)
    await middleware(
        _http_scope(
            path="/project/share/thread-1",
            headers=[(b"host", b"chat.example.com"), (b"x-trace", b"abc")],
        ),
        AsyncMock(),
        send,
    )

    req = captured["request"]
    assert req is not None
    assert req.url.path == "/project/share/thread-1"
    assert req.headers["x-trace"] == "abc"
    assert req.query_params["a"] == "1"
    assert sent[0]["status"] == 200
    assert request() is None


async def test_request_context_middleware_ignores_websocket_scopes():
    captured = {}

    async def inner_app(scope, receive, send):
        captured["request"] = request()

    await RequestContextMiddleware(inner_app)(
        {"type": "websocket"}, AsyncMock(), AsyncMock()
    )

    assert captured["request"] is None


async def test_request_does_not_log_headers_or_cookies(caplog):
    scope = _http_scope(
        headers=[
            (b"host", b"chat.example.com"),
            (b"cookie", b"session=cookie-secret"),
            (b"authorization", b"Bearer auth-secret"),
        ]
    )
    init_ws_context(_websocket_session(scope))

    with caplog.at_level(logging.DEBUG):
        assert request() is not None

    assert "cookie-secret" not in caplog.text
    assert "auth-secret" not in caplog.text


async def test_request_is_exported_from_chainlit():
    import chainlit

    assert chainlit.request is request
    assert "request" in chainlit.__all__
