import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import httpx2
import jwt
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import ValidationError
from starlette.requests import Request

from chainlit.auth import create_jwt, get_current_user
from chainlit.config import (
    SseMcpServer,
    StdioMcpServer,
    StreamableHttpMcpServer,
    config,
)
from chainlit.mcp import (
    _RESTRICTED_HEADERS,
    HttpMcpConnection,
    McpDestinationError,
    SseMcpConnection,
    StdioMcpConnection,
    _destination_in_allowlist,
    _destination_on_origin,
    make_mcp_http_client_factory,
    validate_mcp_headers,
    validate_mcp_url,
)
from chainlit.mcp_oauth import (
    MCP_OAUTH_DETAILS,
    MCP_OAUTH_FAILED_DETAIL,
    MCP_OAUTH_TIMEOUT_DETAIL,
    MCP_OAUTH_TOO_MANY_DETAIL,
    OAUTH_ROLE_EXTENSION,
    TRANSPORT_ROLE,
    McpAuthorizationTimeout,
    McpOAuthOriginError,
    McpOAuthRateLimiter,
    McpOAuthTokenStore,
    PendingAuthorizations,
    canonical_server_key,
)
from chainlit.server import (
    MCP_CONNECT_FAILED_DETAIL,
    MCP_OAUTH_CALLBACK_PATH,
    MCP_OAUTH_CHAINLIT_URL_DETAIL,
    MCP_OAUTH_HEADERS_DETAIL,
    MCP_OAUTH_NO_ORIGINS_DETAIL,
    MCP_OAUTH_RATE_DETAIL,
    MCP_OAUTH_SSE_UNSUPPORTED_DETAIL,
    MCP_OAUTH_URL_DETAIL,
    MCP_OAUTH_WELL_KNOWN_DETAIL,
    MCP_USER_SERVERS_DISABLED_DETAIL,
    app,
    get_mcp_oauth_callback_url,
    get_user_facing_url,
)
from chainlit.session import WebsocketSession
from chainlit.user import User


class TestStdioMcpConnection:
    """Test suite for StdioMcpConnection model."""

    def test_stdio_connection_initialization(self):
        """Test StdioMcpConnection initialization."""
        connection = StdioMcpConnection(
            name="test_server", command="python", args=["-m", "mcp_server"]
        )

        assert connection.name == "test_server"
        assert connection.command == "python"
        assert connection.args == ["-m", "mcp_server"]
        assert connection.clientType == "stdio"

    def test_stdio_connection_with_empty_args(self):
        """Test StdioMcpConnection with empty args list."""
        connection = StdioMcpConnection(name="test_server", command="node", args=[])

        assert connection.args == []
        assert connection.clientType == "stdio"

    def test_stdio_connection_requires_name(self):
        """Test that StdioMcpConnection requires name."""
        with pytest.raises(ValidationError):
            StdioMcpConnection(command="python", args=[])

    def test_stdio_connection_requires_command(self):
        """Test that StdioMcpConnection requires command."""
        with pytest.raises(ValidationError):
            StdioMcpConnection(name="test_server", args=[])

    def test_stdio_connection_requires_args(self):
        """Test that StdioMcpConnection requires args."""
        with pytest.raises(ValidationError):
            StdioMcpConnection(name="test_server", command="python")

    def test_stdio_connection_client_type_is_literal(self):
        """Test that clientType is always 'stdio'."""
        connection = StdioMcpConnection(name="test_server", command="python", args=[])

        assert connection.clientType == "stdio"

    def test_stdio_connection_serialization(self):
        """Test StdioMcpConnection serialization."""
        connection = StdioMcpConnection(
            name="test_server", command="python", args=["-m", "server"]
        )

        data = connection.model_dump()

        assert data["name"] == "test_server"
        assert data["command"] == "python"
        assert data["args"] == ["-m", "server"]
        assert data["clientType"] == "stdio"


class TestSseMcpConnection:
    """Test suite for SseMcpConnection model."""

    def test_sse_connection_initialization(self):
        """Test SseMcpConnection initialization."""
        connection = SseMcpConnection(name="test_server", url="https://example.com/mcp")

        assert connection.name == "test_server"
        assert connection.url == "https://example.com/mcp"
        assert connection.headers is None
        assert connection.clientType == "sse"

    def test_sse_connection_with_headers(self):
        """Test SseMcpConnection with headers."""
        headers = {"Authorization": "Bearer token123", "X-Custom": "value"}
        connection = SseMcpConnection(
            name="test_server", url="https://example.com/mcp", headers=headers
        )

        assert connection.headers == headers

    def test_sse_connection_requires_name(self):
        """Test that SseMcpConnection requires name."""
        with pytest.raises(ValidationError):
            SseMcpConnection(url="https://example.com/mcp")

    def test_sse_connection_requires_url(self):
        """Test that SseMcpConnection requires url."""
        with pytest.raises(ValidationError):
            SseMcpConnection(name="test_server")

    def test_sse_connection_client_type_is_literal(self):
        """Test that clientType is always 'sse'."""
        connection = SseMcpConnection(name="test_server", url="https://example.com/mcp")

        assert connection.clientType == "sse"

    def test_sse_connection_serialization(self):
        """Test SseMcpConnection serialization."""
        headers = {"Authorization": "Bearer token"}
        connection = SseMcpConnection(
            name="test_server", url="https://example.com/mcp", headers=headers
        )

        data = connection.model_dump()

        assert data["name"] == "test_server"
        assert data["url"] == "https://example.com/mcp"
        assert data["headers"] == headers
        assert data["clientType"] == "sse"


class TestHttpMcpConnection:
    """Test suite for HttpMcpConnection model."""

    def test_http_connection_initialization(self):
        """Test HttpMcpConnection initialization."""
        connection = HttpMcpConnection(
            name="test_server", url="https://example.com/mcp"
        )

        assert connection.name == "test_server"
        assert connection.url == "https://example.com/mcp"
        assert connection.headers is None
        assert connection.clientType == "streamable-http"

    def test_http_connection_with_headers(self):
        """Test HttpMcpConnection with headers."""
        headers = {
            "Authorization": "Bearer token123",
            "Content-Type": "application/json",
        }
        connection = HttpMcpConnection(
            name="test_server", url="https://example.com/mcp", headers=headers
        )

        assert connection.headers == headers

    def test_http_connection_requires_name(self):
        """Test that HttpMcpConnection requires name."""
        with pytest.raises(ValidationError):
            HttpMcpConnection(url="https://example.com/mcp")

    def test_http_connection_requires_url(self):
        """Test that HttpMcpConnection requires url."""
        with pytest.raises(ValidationError):
            HttpMcpConnection(name="test_server")

    def test_http_connection_client_type_is_literal(self):
        """Test that clientType is always 'streamable-http'."""
        connection = HttpMcpConnection(
            name="test_server", url="https://example.com/mcp"
        )

        assert connection.clientType == "streamable-http"

    def test_http_connection_serialization(self):
        """Test HttpMcpConnection serialization."""
        headers = {"Authorization": "Bearer token"}
        connection = HttpMcpConnection(
            name="test_server", url="https://example.com/mcp", headers=headers
        )

        data = connection.model_dump()

        assert data["name"] == "test_server"
        assert data["url"] == "https://example.com/mcp"
        assert data["headers"] == headers
        assert data["clientType"] == "streamable-http"


class TestMcpConnectionEdgeCases:
    """Test suite for MCP connection edge cases."""

    def test_stdio_connection_with_complex_args(self):
        """Test StdioMcpConnection with complex arguments."""
        connection = StdioMcpConnection(
            name="complex_server",
            command="python",
            args=[
                "-m",
                "mcp_server",
                "--config",
                "/path/to/config.json",
                "--verbose",
            ],
        )

        assert len(connection.args) == 5
        assert connection.args[0] == "-m"
        assert connection.args[3] == "/path/to/config.json"

    def test_sse_connection_with_multiple_headers(self):
        """Test SseMcpConnection with multiple headers."""
        headers = {
            "Authorization": "Bearer token",
            "X-API-Key": "key123",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        connection = SseMcpConnection(
            name="multi_header_server", url="https://api.example.com", headers=headers
        )

        assert len(connection.headers) == 4
        assert connection.headers["Authorization"] == "Bearer token"
        assert connection.headers["X-API-Key"] == "key123"

    def test_http_connection_with_localhost_url(self):
        """Test HttpMcpConnection with localhost URL."""
        connection = HttpMcpConnection(
            name="local_server", url="http://localhost:8000/mcp"
        )

        assert connection.url == "http://localhost:8000/mcp"

    def test_connection_names_can_be_descriptive(self):
        """Test that connection names can be descriptive strings."""
        stdio_conn = StdioMcpConnection(
            name="My Custom MCP Server (Python)", command="python", args=[]
        )
        sse_conn = SseMcpConnection(
            name="Production API Server", url="https://api.example.com"
        )
        http_conn = HttpMcpConnection(
            name="Development Server - Local", url="http://localhost:3000"
        )

        assert "Python" in stdio_conn.name
        assert "Production" in sse_conn.name
        assert "Development" in http_conn.name


@pytest.mark.parametrize(
    ("url", "allowed_urls", "should_raise"),
    [
        ("https://api.example.com/mcp", ["https://api.example.com/mcp"], False),
        ("https://api.example.com/mcp/tools", ["https://api.example.com/mcp"], False),
        ("https://API.example.com/mcp", ["https://api.example.com/mcp"], False),
        (
            "https://api.example.com:443/mcp",
            ["https://api.example.com/mcp"],
            False,
        ),
        ("https://example.com/any/path", ["https://example.com"], False),
        ("https://evil.com/mcp", ["https://api.example.com/mcp"], True),
        ("https://api.example.com/mcp", [], True),
        # Scheme, port and path-subtree boundaries.
        ("http://api.example.com/mcp", ["https://api.example.com/mcp"], True),
        (
            "https://api.example.com:9000/mcp",
            ["https://api.example.com:8000/mcp"],
            True,
        ),
        ("https://api.example.com/v1-evil", ["https://api.example.com/v1"], True),
        ("https://api.example.com/v1beta", ["https://api.example.com/v1"], True),
        ("file:///etc/passwd", ["https://allowed.com/api"], True),
        # Ambiguous paths must be rejected, never normalised.
        ("https://allowed.com/api/../../admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/./../admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/%2e%2e/admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api%2f..%2fadmin", ["https://allowed.com/api"], True),
        (
            "https://allowed.com/api/%252e%252e/admin",
            ["https://allowed.com/api"],
            True,
        ),
        ("https://allowed.com/api/..%5cadmin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/..\\admin", ["https://allowed.com/api"], True),
        (
            "https://allowed.com/api/\uff0e\uff0e/admin",
            ["https://allowed.com/api"],
            True,
        ),
        (
            "https://allowed.com/api/%EF%BC%8E%EF%BC%8E/admin",
            ["https://allowed.com/api"],
            True,
        ),
        (
            "https://allowed.com/api/\u2024\u2024/admin",
            ["https://allowed.com/api"],
            True,
        ),
        ("https://allowed.com/api/\uff0f../admin", ["https://allowed.com/api"], True),
        # Ambiguities that only appear at some origins: path parameters,
        # NUL/control truncation, encoded ';', and IIS %u escapes.
        ("https://allowed.com/api/..;/admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/..%00/admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/..%3b/admin", ["https://allowed.com/api"], True),
        (
            "https://allowed.com/api/%u002e%u002e/admin",
            ["https://allowed.com/api"],
            True,
        ),
        ("https://allowed.com/api/..%7f/admin", ["https://allowed.com/api"], True),
        # A ';params' tail must not hide traversal: urlsplit keeps it in the
        # path, urlparse would strip it into .params.
        (
            "https://allowed.com/api/x;%5c..%5c..%5cadmin",
            ["https://allowed.com/api"],
            True,
        ),
        ("https://allowed.com/api/x;%2f..%2fadmin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/x;%2e%2e/admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/x;%00/admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/x;%25admin", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/x;../../admin", ["https://allowed.com/api"], True),
        # ';' in the final segment is the form urlparse hides; keep one case
        # per class so the parse cannot silently regress.
        ("https://allowed.com/api/x;%2e%2e", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/x;%00", ["https://allowed.com/api"], True),
        ("https://allowed.com/api/x;..", ["https://allowed.com/api"], True),
        # A legitimate percent-encoded path segment must still pass.
        ("https://allowed.com/api/name%20x", ["https://allowed.com/api"], False),
    ],
)
def test_validate_mcp_url(url, allowed_urls, should_raise):
    if should_raise:
        with pytest.raises(ValueError, match="not in the allowed"):
            validate_mcp_url(url, allowed_urls)
    else:
        validate_mcp_url(url, allowed_urls)


class TestValidateMcpHeaders:
    @pytest.mark.parametrize("header_name", sorted(_RESTRICTED_HEADERS))
    def test_restricted_headers_are_stripped(self, header_name):
        result = validate_mcp_headers({header_name: "value", "X-Custom": "keep"})

        assert result == {"X-Custom": "keep"}

    @pytest.mark.parametrize("header_name", ["COOKIE", "X-Forwarded-For"])
    def test_restricted_headers_are_case_insensitive(self, header_name):
        assert validate_mcp_headers({header_name: "value"}) is None

    @pytest.mark.parametrize(
        "header_name",
        [
            "X-Forwarded-User",
            "X_Forwarded_For",
            "X-Original-Host",
            "X-Rewrite-URL",
            "X-Forwarded-Client-Cert",
            "X-Scheme",
        ],
    )
    def test_proxy_identity_prefixes_are_stripped(self, header_name):
        assert validate_mcp_headers({header_name: "spoof", "X-Custom": "keep"}) == {
            "X-Custom": "keep"
        }

    def test_authorization_survives(self):
        headers = {"Authorization": "Bearer secret-token"}

        assert validate_mcp_headers(headers) == headers

    def test_empty_results_become_none(self):
        assert validate_mcp_headers(None) is None
        assert validate_mcp_headers({}) is None
        assert validate_mcp_headers({"Cookie": "a=b", "Host": "evil.com"}) is None


class TestMakeMcpHttpClientFactory:
    async def test_disables_redirects_and_sets_default_timeout(self):
        client = make_mcp_http_client_factory(lambda _url: None)()
        try:
            assert client.follow_redirects is False
            assert client.timeout == httpx2.Timeout(30.0)
        finally:
            await client.aclose()

    async def test_passed_headers_timeout_and_auth_are_honored(self):
        class DummyAuth(httpx2.Auth):
            def auth_flow(self, request):
                yield request

        auth = DummyAuth()
        timeout = httpx2.Timeout(5.0)
        client = make_mcp_http_client_factory(lambda _url: None)(
            headers={"X-Test": "value"}, timeout=timeout, auth=auth
        )
        try:
            assert client.headers["x-test"] == "value"
            assert client.timeout == timeout
            assert client.auth is auth
        finally:
            await client.aclose()

    async def test_every_request_is_checked_and_blocked_with_on_blocked(self):
        calls: list[str] = []
        blocked: list[McpDestinationError] = []

        def check(url: str) -> None:
            calls.append(url)
            if "evil" in url:
                raise McpDestinationError(f"blocked: {url}")

        client = make_mcp_http_client_factory(check, on_blocked=blocked.append)()
        try:
            hook = client.event_hooks["request"][0]
            await hook(httpx2.Request("GET", "https://good.example.com/x"))
            with pytest.raises(McpDestinationError):
                await hook(httpx2.Request("GET", "https://evil.example.com/x"))
        finally:
            await client.aclose()

        assert calls == [
            "https://good.example.com/x",
            "https://evil.example.com/x",
        ]
        assert len(blocked) == 1
        assert "evil.example.com" in str(blocked[0])


class TestDestinationGrants:
    async def test_allowlist_hook_blocks_out_of_subtree_and_allows_subtree(self):
        client = make_mcp_http_client_factory(
            _destination_in_allowlist(["https://allowed.com/mcp/users/alice"])
        )()
        try:
            hook = client.event_hooks["request"][0]
            with pytest.raises(McpDestinationError):
                await hook(httpx2.Request("POST", "https://allowed.com/admin"))
            await hook(
                httpx2.Request("POST", "https://allowed.com/mcp/users/alice/messages")
            )
        finally:
            await client.aclose()

    async def test_origin_hook_allows_sibling_path_and_blocks_cross_origin(self):
        client = make_mcp_http_client_factory(
            _destination_on_origin("https://server.example.com/sse")
        )()
        try:
            hook = client.event_hooks["request"][0]
            await hook(httpx2.Request("POST", "https://server.example.com/messages/"))
            with pytest.raises(McpDestinationError):
                await hook(httpx2.Request("POST", "https://evil.example.com/messages/"))
        finally:
            await client.aclose()


ALICE = User(identifier="alice")
BOB = User(identifier="bob")
SERVER_KEY = "https://mcp.example.com/mcp"


def login_as(user: User) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def make_request(path: str = "/mcp") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("127.0.0.1", 8000),
            "root_path": "",
            "path": path,
            "query_string": b"",
            "headers": [],
        }
    )


def make_tokens(access: str) -> OAuthToken:
    return OAuthToken(access_token=access, refresh_token=f"{access}-refresh")


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def mcp_pending(monkeypatch):
    flows = PendingAuthorizations()
    monkeypatch.setattr("chainlit.mcp_oauth.get_pending_authorizations", lambda: flows)
    yield flows
    for state in list(flows._pending):
        flows.cancel(state)


@pytest.fixture
def mcp_store(monkeypatch):
    store = McpOAuthTokenStore()
    monkeypatch.setattr("chainlit.mcp_oauth.get_token_store", lambda: store)
    return store


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setenv("CHAINLIT_CUSTOM_AUTH", "1")
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "test-secret-with-at-least-32-bytes!!")


@pytest.fixture
def logout_calls(monkeypatch):
    calls: list[Request] = []

    async def on_logout(request: Request, response):
        calls.append(request)
        return {"success": True, "from": "callback"}

    monkeypatch.setattr(config.code, "on_logout", on_logout)
    return calls


class TestMcpOAuthCallback:
    async def test_oauth_callback_rejects_an_unauthenticated_request(self, client):
        response = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "c", "state": "s"}
        )

        assert response.status_code == 401

    async def test_oauth_callback_rejects_missing_code_or_state(
        self, client, mcp_pending
    ):
        login_as(ALICE)

        for params in ({}, {"state": "s"}, {"code": "c"}):
            response = await client.get(MCP_OAUTH_CALLBACK_PATH, params=params)
            assert response.status_code == 400
            assert response.json()["detail"] in MCP_OAUTH_DETAILS

    async def test_oauth_callback_rejects_an_unknown_state(self, client, mcp_pending):
        login_as(ALICE)

        response = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "c", "state": "state-abc123"}
        )

        assert response.status_code == 400
        assert response.json()["detail"] in MCP_OAUTH_DETAILS
        assert "state-abc123" not in response.text

    async def test_oauth_callback_rejects_an_expired_state(self, client, monkeypatch):
        login_as(ALICE)
        flows = PendingAuthorizations(ttl_seconds=0.01)
        monkeypatch.setattr(
            "chainlit.mcp_oauth.get_pending_authorizations", lambda: flows
        )
        flows.register("stale", "alice", SERVER_KEY)
        await asyncio.sleep(0.02)

        response = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "c", "state": "stale"}
        )

        assert response.status_code == 400
        assert response.json()["detail"] in MCP_OAUTH_DETAILS
        assert "stale" not in response.text

    async def test_oauth_callback_accepts_a_state_once(self, client, mcp_pending):
        login_as(ALICE)
        mcp_pending.register("state-1", "alice", SERVER_KEY)

        first = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "c", "state": "state-1"}
        )
        replay = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "c", "state": "state-1"}
        )

        assert first.status_code == 200
        assert replay.status_code == 400
        assert replay.json()["detail"] in MCP_OAUTH_DETAILS

    async def test_oauth_callback_refuses_another_users_state_without_consuming_it(
        self, client, mcp_pending
    ):
        mcp_pending.register("owner-state", "alice", SERVER_KEY)
        login_as(BOB)

        refused = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "c", "state": "owner-state"}
        )

        assert refused.status_code == 403
        assert refused.json()["detail"] in MCP_OAUTH_DETAILS
        assert "owner-state" not in refused.text
        assert "alice" not in refused.text

        login_as(ALICE)
        accepted = await client.get(
            MCP_OAUTH_CALLBACK_PATH,
            params={"code": "the-code", "state": "owner-state"},
        )
        assert accepted.status_code == 200

    async def test_oauth_callback_returns_a_tokenless_html_page(
        self, client, mcp_pending
    ):
        login_as(ALICE)
        handle = mcp_pending.register("state-1", "alice", SERVER_KEY)

        response = await client.get(
            MCP_OAUTH_CALLBACK_PATH,
            params={"code": "secret-code", "state": "state-1"},
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["content-type"].startswith("text/html")
        assert "secret-code" not in response.text
        assert "state-1" not in response.text
        assert "access_token" not in response.text
        assert handle._future.done()


class TestMcpOAuthCallbackUrl:
    def test_oauth_callback_url_shares_origin_and_prefix_with_login(self, monkeypatch):
        monkeypatch.setenv("CHAINLIT_URL", "https://app.example.com")
        monkeypatch.setenv("CHAINLIT_ROOT_PATH", "/chainlit")
        request = make_request("/chainlit/mcp")

        callback = get_mcp_oauth_callback_url(request)
        login = (
            get_user_facing_url(request.url.replace(path="/chainlit/auth/oauth/github"))
            + "/callback"
        )

        assert callback == "https://app.example.com/chainlit/mcp/oauth/callback"
        assert urlsplit(callback)[:2] == urlsplit(login)[:2]
        assert urlsplit(login).path == "/chainlit/auth/oauth/github/callback"

    def test_oauth_callback_url_keeps_root_path_without_chainlit_url(self, monkeypatch):
        monkeypatch.delenv("CHAINLIT_URL", raising=False)
        monkeypatch.setenv("CHAINLIT_ROOT_PATH", "/chainlit")

        callback = get_mcp_oauth_callback_url(make_request("/chainlit/mcp"))

        assert callback == "http://127.0.0.1:8000/chainlit/mcp/oauth/callback"


class TestLogout:
    async def test_it_forgets_the_users_tokens_and_pending_flows(
        self, client, auth_enabled, mcp_store, mcp_pending, logout_calls
    ):
        await mcp_store.scoped("alice", SERVER_KEY).set_tokens(make_tokens("alice-at"))
        await mcp_store.scoped("bob", SERVER_KEY).set_tokens(make_tokens("bob-at"))
        mcp_pending.register("alice-flow", "alice", SERVER_KEY)
        mcp_pending.register("bob-flow", "bob", SERVER_KEY)

        client.cookies.set("access_token", create_jwt(ALICE))

        response = await client.post("/logout")

        assert response.status_code == 200
        assert response.json() == {"success": True, "from": "callback"}
        assert len(logout_calls) == 1
        assert "access_token" in response.headers.get("set-cookie", "")
        assert await mcp_store.scoped("alice", SERVER_KEY).get_tokens() is None
        with pytest.raises(KeyError):
            mcp_pending.resolve("alice-flow", "alice", "code")
        # Another user's credentials and flow are untouched.
        assert await mcp_store.scoped("bob", SERVER_KEY).get_tokens() is not None
        mcp_pending.resolve("bob-flow", "bob", "code")

    @pytest.mark.parametrize("token", ["", "garbage", "expired"])
    async def test_it_clears_the_cookie_and_calls_on_logout_for_any_token(
        self, client, auth_enabled, logout_calls, token, monkeypatch
    ):
        if token == "expired":
            token = jwt.encode(
                {
                    "identifier": "alice",
                    "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
                },
                "test-secret-with-at-least-32-bytes!!",
                algorithm="HS256",
            )

        if token:
            client.cookies.set("access_token", token)

        response = await client.post("/logout")

        assert response.status_code == 200
        assert len(logout_calls) == 1
        if token:
            assert "access_token" in response.headers.get("set-cookie", "")

    async def test_a_raising_on_logout_still_clears_cookie_and_tokens(
        self, client, auth_enabled, mcp_store, logout_calls, monkeypatch
    ):
        await mcp_store.scoped("alice", SERVER_KEY).set_tokens(make_tokens("alice-at"))

        async def raising(request, response):
            raise RuntimeError("on_logout exploded")

        monkeypatch.setattr(config.code, "on_logout", raising)

        client.cookies.set("access_token", create_jwt(ALICE))

        response = await client.post("/logout")

        assert response.status_code == 200
        assert "access_token" in response.headers.get("set-cookie", "")
        assert await mcp_store.scoped("alice", SERVER_KEY).get_tokens() is None

    async def test_it_leaves_the_store_alone_without_a_cookie(self, client, mcp_store):
        await mcp_store.scoped("alice", SERVER_KEY).set_tokens(make_tokens("alice-at"))

        response = await client.post("/logout")

        assert response.status_code == 200
        assert await mcp_store.scoped("alice", SERVER_KEY).get_tokens() is not None

    async def test_it_leaves_the_store_alone_with_auth_enabled_but_no_cookie(
        self, client, auth_enabled, mcp_store, logout_calls
    ):
        await mcp_store.scoped("alice", SERVER_KEY).set_tokens(make_tokens("alice-at"))

        response = await client.post("/logout")

        assert response.status_code == 200
        assert len(logout_calls) == 1
        assert await mcp_store.scoped("alice", SERVER_KEY).get_tokens() is not None


@pytest.fixture
async def ws_session():
    session = WebsocketSession(
        id="mcp-session",
        socket_id="mcp-socket",
        emit=AsyncMock(),
        emit_call=AsyncMock(),
        user_env={},
        client_type="webapp",
        user=ALICE,
    )
    yield session
    await session.delete()


class TestMcpConnectErrorBody:
    async def test_hostile_server_text_is_logged_but_not_echoed(
        self, client, test_config, ws_session, monkeypatch, caplog
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            StreamableHttpMcpServer(
                name="weather", type="streamable-http", url=SERVER_KEY
            )
        ]
        login_as(ALICE)
        hostile = "PRM body https://evil.example/token?secret=hostile-value"

        def explode(*args, **kwargs):
            raise RuntimeError(hostile)

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client", explode
        )

        with caplog.at_level(logging.WARNING):
            response = await client.post(
                "/mcp",
                json={"sessionId": ws_session.id, "name": "weather"},
            )

        assert response.status_code == 400
        assert response.json() == {"detail": MCP_CONNECT_FAILED_DETAIL}
        assert hostile not in response.text
        assert "hostile-value" in caplog.text


OAUTH_SERVER = "https://as.example"
OAUTH_MCP_URL = "https://mcp.example.com/mcp"


def make_client_info(client_id: str = "client-id"):
    return OAuthClientInformationFull(
        client_id=client_id, client_secret="client-secret"
    )


async def wait_until(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(0.01)


class FakeAsyncClient:
    instances: list["FakeAsyncClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.auth = kwargs.get("auth")
        self.event_hooks = kwargs.get("event_hooks")
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeClientSession:
    initialize_behavior = None
    tools = [SimpleNamespace(name="echo")]

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        if type(self).initialize_behavior is not None:
            await type(self).initialize_behavior(self)

    async def list_tools(self):
        return SimpleNamespace(tools=type(self).tools)


class FakeOAuthFlow:
    next_state = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.provider = httpx2.Auth()
        state = f"fake-state-{FakeOAuthFlow.next_state}"
        FakeOAuthFlow.next_state += 1
        # The real provider registers the flow from its redirect handler; the
        # pending entry must exist for the whole browser sign-in.
        self.handle = kwargs["pending"].register(
            state,
            kwargs["user_identifier"],
            canonical_server_key(kwargs["server_url"]),
            session_id=kwargs["session_id"],
            name=kwargs["name"],
            max_per_user=kwargs.get("max_pending"),
        )
        self._authorization = self.handle
        self.url = f"{OAUTH_SERVER}/authorize?state={state}"
        self.url_published = asyncio.Event()

    async def wait_for_authorization_url(self, timeout: float | None = None) -> str:
        if self.kwargs.get("on_redirect") is not None:
            await self.kwargs["on_redirect"](self.url)
        self.url_published.set()
        return self.url


@pytest.fixture
def oauth_config(test_config, monkeypatch):
    test_config.features.mcp.enabled = True
    test_config.features.mcp.oauth_authorization_origins = [OAUTH_SERVER]
    test_config.features.mcp.user_oauth.enabled = True
    # The OAuth branch is a user-provided connection: it is gated by the same
    # user_servers switch and allowlist as the plain SSE/HTTP path.
    test_config.features.mcp.user_servers.enabled = True
    test_config.features.mcp.user_servers.allowed_urls = ["https://mcp.example.com"]
    test_config.features.mcp.user_oauth.max_starts_per_user_per_minute = 5
    test_config.features.mcp.user_oauth.max_pending_per_user = 3
    monkeypatch.setenv("CHAINLIT_URL", "https://app.example.com")
    return test_config


@pytest.fixture
def mcp_rate_limiter(monkeypatch):
    limiter = McpOAuthRateLimiter()
    monkeypatch.setattr("chainlit.mcp_oauth.get_rate_limiter", lambda: limiter)
    return limiter


@pytest.fixture
def fake_flows(monkeypatch):
    flows: list[FakeOAuthFlow] = []
    monkeypatch.setattr(FakeOAuthFlow, "next_state", 0)

    def build(**kwargs):
        flow = FakeOAuthFlow(**kwargs)
        flows.append(flow)
        return flow

    monkeypatch.setattr("chainlit.mcp_oauth.build_oauth_provider", build)
    return flows


@pytest.fixture
def fake_sdk(monkeypatch):
    clients: list[FakeAsyncClient] = []
    FakeAsyncClient.instances = clients
    FakeClientSession.initialize_behavior = None
    FakeClientSession.tools = [SimpleNamespace(name="echo")]

    transport_calls = []

    @contextlib.asynccontextmanager
    async def fake_streamable(url, http_client):
        transport_calls.append((url, http_client))
        yield (AsyncMock(), AsyncMock())

    monkeypatch.setattr("httpx2.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("mcp.ClientSession", FakeClientSession)
    monkeypatch.setattr(
        "mcp.client.streamable_http.streamable_http_client", fake_streamable
    )
    return SimpleNamespace(
        clients=clients, transport=transport_calls, session_cls=FakeClientSession
    )


def oauth_body(ws_session, **overrides) -> dict:
    body = {
        "sessionId": ws_session.id,
        "clientType": "streamable-http",
        "name": "weather",
        "url": OAUTH_MCP_URL,
        "useOAuth": True,
    }
    body.update(overrides)
    return body


def resolve_pending_on_initialize(flows):
    async def behavior(_session):
        flow = flows[-1]
        # A browser cannot answer before the URL is shown; keep the socket
        # event order (authorization_required, then connected).
        await flow.url_published.wait()
        flow.kwargs["pending"].resolve(
            flow.handle.state, flow.kwargs["user_identifier"], "the-code"
        )

    return behavior


async def park_on_initialize(_session):
    await asyncio.Event().wait()


class TestMcpOAuthGates:
    async def test_empty_origins_is_refused_before_any_request(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.oauth_authorization_origins = []
        login_as(ALICE)

        for url in (OAUTH_MCP_URL, f"{OAUTH_SERVER}/mcp"):
            response = await client.post("/mcp", json=oauth_body(ws_session, url=url))
            assert response.status_code == 400
            assert response.json()["detail"] == MCP_OAUTH_NO_ORIGINS_DETAIL

        assert fake_sdk.transport == []
        assert fake_flows == []

    async def test_the_second_opt_in_is_required(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.user_oauth.enabled = False
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 400
        assert "features.mcp.user_oauth.enabled" in response.json()["detail"]
        assert fake_sdk.transport == []

    async def test_oauth_without_authentication_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 401
        assert fake_sdk.transport == []

    async def test_sse_oauth_is_refused_without_calling_sse_client(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        sse_calls = []
        monkeypatch.setattr(
            "mcp.client.sse.sse_client", lambda *a, **k: sse_calls.append((a, k))
        )
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json=oauth_body(ws_session, clientType="sse", url=OAUTH_MCP_URL),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_SSE_UNSUPPORTED_DETAIL
        assert sse_calls == []
        assert fake_sdk.transport == []

    async def test_stdio_is_rejected_at_validation_without_calling_stdio_client(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        monkeypatch,
    ):
        stdio_calls = []
        monkeypatch.setattr(
            "mcp.client.stdio.stdio_client", lambda *a, **k: stdio_calls.append((a, k))
        )
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "clientType": "stdio",
                "name": "evil",
                "fullCommand": "npx -y example",
            },
        )

        # `clientType: "stdio"` is not part of the request model any more and
        # `fullCommand` is a forbidden extra field, so the request never
        # reaches the handler that could spawn a subprocess.
        assert response.status_code == 422
        assert stdio_calls == []
        assert fake_sdk.transport == []

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Bearer token"},
            {"authorization": "Bearer token"},
            {"Proxy-Authorization": "Basic token"},
            {"Cookie": "session=1"},
            {"X-Api-Key": "static-key"},
            {"X-Custom": "value", "Content-Type": "application/json"},
        ],
    )
    async def test_any_headers_are_refused_with_oauth(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        headers,
    ):
        login_as(ALICE)

        response = await client.post(
            "/mcp", json=oauth_body(ws_session, headers=headers)
        )

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_HEADERS_DETAIL
        assert fake_sdk.transport == []

    async def test_a_plain_connection_outside_the_allowlist_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.user_servers.allowed_urls = []
        login_as(ALICE)
        headers = {"X-Api-Key": "static-key"}

        response = await client.post(
            "/mcp", json=oauth_body(ws_session, useOAuth=False, headers=headers)
        )

        assert response.status_code == 400
        assert "not in the allowed" in response.json()["detail"]
        assert fake_sdk.clients == []
        assert fake_sdk.transport == []

    async def test_chainlit_url_is_required(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        monkeypatch.delenv("CHAINLIT_URL", raising=False)
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_CHAINLIT_URL_DETAIL
        assert fake_sdk.transport == []

    async def test_plain_http_chainlit_url_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        monkeypatch.setenv("CHAINLIT_URL", "http://10.0.0.5:8000")
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_CHAINLIT_URL_DETAIL
        assert fake_sdk.transport == []

    async def test_a_loopback_chainlit_url_is_allowed(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        monkeypatch.setenv("CHAINLIT_URL", "http://localhost:8000")
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 202
        assert response.json()["status"] == "authorization_required"

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.5/mcp",
            "ftp://mcp.example.com/mcp",
            "https://alice:secret@mcp.example.com/mcp",
        ],
    )
    async def test_an_invalid_oauth_mcp_url_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        url,
    ):
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session, url=url))

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_URL_DETAIL
        assert fake_sdk.transport == []

    @pytest.mark.parametrize(
        "path",
        [
            "/.well-known/oauth-authorization-server",
            "/.WELL-KNOWN/openid-configuration",
            "/%2Ewell-known/oauth-authorization-server",
            "/nested/.Well-Known/tenant",
        ],
    )
    async def test_a_well_known_mcp_path_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        path,
    ):
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json=oauth_body(ws_session, url=f"https://mcp.example.com{path}"),
        )

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_WELL_KNOWN_DETAIL
        assert fake_sdk.transport == []

    async def test_the_sixth_start_in_a_minute_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.user_oauth.max_pending_per_user = 10
        login_as(ALICE)

        for index in range(5):
            response = await client.post(
                "/mcp", json=oauth_body(ws_session, name=f"weather-{index}")
            )
            assert response.status_code == 202

        refused = await client.post(
            "/mcp", json=oauth_body(ws_session, name="weather-5")
        )
        assert refused.status_code == 429
        assert refused.json()["detail"] == MCP_OAUTH_RATE_DETAIL

        plain = await client.post(
            "/mcp", json=oauth_body(ws_session, name="plain", useOAuth=False)
        )
        # A plain (non-OAuth) connect is not subject to the OAuth limiter.
        assert plain.status_code == 200

    async def test_the_fourth_pending_flow_is_refused(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.user_oauth.max_starts_per_user_per_minute = 100
        oauth_config.features.mcp.user_oauth.max_pending_per_user = 3
        login_as(ALICE)

        for index in range(3):
            response = await client.post(
                "/mcp", json=oauth_body(ws_session, name=f"weather-{index}")
            )
            assert response.status_code == 202

        refused = await client.post(
            "/mcp", json=oauth_body(ws_session, name="weather-4")
        )
        assert refused.status_code == 429
        assert refused.json()["detail"] == MCP_OAUTH_TOO_MANY_DETAIL

        plain = await client.post(
            "/mcp", json=oauth_body(ws_session, name="plain", useOAuth=False)
        )
        # A plain (non-OAuth) connect is not subject to the OAuth limiter.
        assert plain.status_code == 200


class TestMcpOAuthConnect:
    async def test_success_returns_202_then_emits_connected(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = resolve_pending_on_initialize(
            fake_flows
        )
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "authorization_required"
        assert body["url"].startswith(f"{OAUTH_SERVER}/authorize?")
        assert body["mcp"]["name"] == "weather"
        assert body["mcp"]["tools"] == []

        await wait_until(lambda: "weather" in ws_session.mcp_sessions)
        events = [call.args for call in ws_session.emit.call_args_list]
        names = [event[0] for event in events]
        assert names[0] == "mcp_authorization_required"
        assert names.count("mcp_connected") == 1
        assert "mcp_connection_failed" not in names
        assert mcp_pending.get_for_session(ws_session.id, "weather") is None
        assert ws_session.mcp_oauth_tasks == {}
        assert ws_session.mcp_sessions[
            "weather"
        ].oauth_server_key == canonical_server_key(OAUTH_MCP_URL)

    async def test_a_stored_token_connects_without_a_redirect(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        async def never(_self, timeout: float | None = None) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(
            FakeOAuthFlow, "wait_for_authorization_url", never, raising=True
        )
        fake_sdk.session_cls.initialize_behavior = None
        login_as(ALICE)
        storage = mcp_store.scoped("alice", canonical_server_key(OAUTH_MCP_URL))
        await storage.set_tokens(
            OAuthToken(access_token="stored-at", refresh_token="stored-rt")
        )

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["mcp"]["name"] == "weather"
        assert body["mcp"]["tools"] == [{"name": "echo"}]
        await wait_until(
            lambda: any(
                call.args[0] == "mcp_connected"
                for call in ws_session.emit.call_args_list
            )
        )
        names = [call.args[0] for call in ws_session.emit.call_args_list]
        assert names.count("mcp_connected") == 1
        assert "mcp_authorization_required" not in names
        assert "mcp_connection_failed" not in names
        assert "weather" in ws_session.mcp_sessions
        assert ws_session.mcp_oauth_tasks == {}
        assert not ws_session.mcp_sessions["weather"].task.done()
        assert (await storage.get_tokens()).access_token == "stored-at"

    async def test_a_failure_before_the_url_returns_the_fixed_detail(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        async def never(_self, timeout: float | None = None) -> None:
            await asyncio.Event().wait()

        async def explode(_session):
            raise RuntimeError("internal detail")

        monkeypatch.setattr(
            FakeOAuthFlow, "wait_for_authorization_url", never, raising=True
        )
        fake_sdk.session_cls.initialize_behavior = explode
        login_as(ALICE)

        started = asyncio.get_running_loop().time()
        response = await client.post("/mcp", json=oauth_body(ws_session))
        elapsed = asyncio.get_running_loop().time() - started

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_OAUTH_FAILED_DETAIL
        assert elapsed < 5.0
        events = [call.args for call in ws_session.emit.call_args_list]
        names = [event[0] for event in events]
        failures = [event for event in events if event[0] == "mcp_connection_failed"]
        assert names.count("mcp_connection_failed") == 1
        assert failures[0][1] == {
            "name": "weather",
            "detail": MCP_OAUTH_FAILED_DETAIL,
        }
        assert "mcp_authorization_required" not in names
        assert "mcp_connected" not in names
        assert ws_session.mcp_sessions == {}
        assert ws_session.mcp_oauth_tasks == {}
        assert mcp_pending.get_for_session(ws_session.id, "weather") is None

    async def test_a_failure_emits_one_fixed_detail(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        async def explode(_session):
            raise RuntimeError("hostile https://evil.example/token?secret=value")

        fake_sdk.session_cls.initialize_behavior = explode
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202

        await wait_until(
            lambda: any(
                call.args[0] == "mcp_connection_failed"
                for call in ws_session.emit.call_args_list
            )
        )
        events = [call.args for call in ws_session.emit.call_args_list]
        failures = [event for event in events if event[0] == "mcp_connection_failed"]
        assert len(failures) == 1
        assert failures[0][1] == {
            "name": "weather",
            "detail": MCP_OAUTH_FAILED_DETAIL,
        }
        assert "hostile" not in repr(events)
        assert mcp_pending.get_for_session(ws_session.id, "weather") is None
        assert ws_session.mcp_sessions == {}
        assert ws_session.mcp_oauth_tasks == {}

    async def test_a_timeout_uses_the_fixed_timeout_detail(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        async def expire(_session):
            raise McpAuthorizationTimeout("internal detail")

        fake_sdk.session_cls.initialize_behavior = expire
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202

        await wait_until(
            lambda: any(
                call.args[0] == "mcp_connection_failed"
                for call in ws_session.emit.call_args_list
            )
        )
        events = [call.args for call in ws_session.emit.call_args_list]
        failure = next(event for event in events if event[0] == "mcp_connection_failed")
        assert failure[1]["detail"] == MCP_OAUTH_TIMEOUT_DETAIL
        assert "internal detail" not in repr(events)

    async def test_cancelling_before_a_token_discards_client_info(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = park_on_initialize
        login_as(ALICE)
        server_key = canonical_server_key(OAUTH_MCP_URL)
        storage = mcp_store.scoped("alice", server_key)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202
        await wait_until(lambda: bool(fake_sdk.clients))
        await storage.set_client_info(make_client_info())

        task = ws_session.mcp_oauth_tasks["weather"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None
        assert mcp_pending.get_for_session(ws_session.id, "weather") is None
        assert ws_session.mcp_sessions == {}

    async def test_cancelling_does_not_delete_a_concurrent_token(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = park_on_initialize
        login_as(ALICE)
        server_key = canonical_server_key(OAUTH_MCP_URL)
        storage = mcp_store.scoped("alice", server_key)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202
        await wait_until(lambda: bool(fake_sdk.clients))
        await storage.set_client_info(make_client_info())
        await storage.set_tokens(
            OAuthToken(access_token="concurrent-at", refresh_token="concurrent-rt")
        )

        task = ws_session.mcp_oauth_tasks["weather"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        tokens = await storage.get_tokens()
        assert tokens is not None
        assert tokens.access_token == "concurrent-at"
        assert await storage.get_client_info() is not None

    async def test_delete_during_sign_in_cancels_the_flow_and_forgets_the_key(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = park_on_initialize
        login_as(ALICE)
        server_key = canonical_server_key(OAUTH_MCP_URL)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202
        state = fake_flows[0].handle.state
        await mcp_store.scoped("alice", server_key).set_client_info(make_client_info())

        deleted = await client.request(
            "DELETE",
            "/mcp",
            json={"sessionId": ws_session.id, "name": "weather"},
        )

        assert deleted.status_code == 200
        assert mcp_pending.get_for_session(ws_session.id, "weather") is None
        assert ws_session.mcp_oauth_tasks == {}
        assert ws_session.mcp_sessions == {}
        assert await mcp_store.scoped("alice", server_key).get_client_info() is None

        callback = await client.get(
            MCP_OAUTH_CALLBACK_PATH, params={"code": "late", "state": state}
        )
        assert callback.status_code == 400
        assert callback.json()["detail"] in MCP_OAUTH_DETAILS

    async def test_delete_after_connect_uses_the_session_key(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = resolve_pending_on_initialize(
            fake_flows
        )
        login_as(ALICE)
        server_key = canonical_server_key(OAUTH_MCP_URL)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202
        await wait_until(lambda: "weather" in ws_session.mcp_sessions)

        storage = mcp_store.scoped("alice", server_key)
        await storage.set_tokens(
            OAuthToken(access_token="access-token", refresh_token="refresh-token")
        )
        await storage.set_client_info(make_client_info())

        deleted = await client.request(
            "DELETE",
            "/mcp",
            json={"sessionId": ws_session.id, "name": "weather"},
        )

        assert deleted.status_code == 200
        assert "weather" not in ws_session.mcp_sessions
        assert await storage.get_tokens() is None
        assert await storage.get_client_info() is None

    async def test_a_second_connect_supersedes_the_first(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = park_on_initialize
        login_as(ALICE)

        first = await client.post("/mcp", json=oauth_body(ws_session))
        assert first.status_code == 202
        first_task = ws_session.mcp_oauth_tasks["weather"]
        first_state = fake_flows[0].handle.state

        second = await client.post("/mcp", json=oauth_body(ws_session))
        assert second.status_code == 202

        await wait_until(lambda: first_task.done())
        pending = mcp_pending.get_for_session(ws_session.id, "weather")
        assert pending is not None
        assert pending.state != first_state
        assert len(mcp_pending) == 1
        assert len(ws_session.mcp_oauth_tasks) == 1
        assert ws_session.mcp_sessions == {}

    async def test_session_close_cancels_the_flow(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = park_on_initialize
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))
        assert response.status_code == 202
        task = ws_session.mcp_oauth_tasks["weather"]

        await ws_session.delete()

        assert task.done()
        assert mcp_pending.get_for_session(ws_session.id, "weather") is None
        assert ws_session.mcp_oauth_tasks == {}
        assert ws_session.mcp_sessions == {}

    async def test_the_guard_is_attached_only_to_the_oauth_client(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        fake_sdk.session_cls.initialize_behavior = resolve_pending_on_initialize(
            fake_flows
        )
        login_as(ALICE)

        oauth = await client.post("/mcp", json=oauth_body(ws_session))
        assert oauth.status_code == 202
        await wait_until(lambda: bool(fake_sdk.clients))
        oauth_client = fake_sdk.clients[0]
        assert oauth_client.auth is not None
        assert oauth_client.event_hooks is not None
        assert len(oauth_client.event_hooks["request"]) == 1
        await wait_until(lambda: "weather" in ws_session.mcp_sessions)
        fake_sdk.session_cls.initialize_behavior = None

        plain = await client.post(
            "/mcp", json=oauth_body(ws_session, name="plain", useOAuth=False)
        )
        # The plain path is not OAuth, so it gets a destination-checking client
        # without the OAuth request guard (which bounds AS/PRM traffic).
        assert plain.status_code == 200
        assert len(fake_sdk.clients) == 2
        plain_client = fake_sdk.clients[1]
        assert plain_client is not oauth_client
        assert plain_client.auth is None
        assert len(plain_client.event_hooks["request"]) == 1


class TestMcpPlainRegression:
    async def test_configured_server_success_body_exposes_only_name_type_and_tools(
        self,
        client,
        test_config,
        ws_session,
        fake_sdk,
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            StreamableHttpMcpServer(
                name="weather",
                type="streamable-http",
                url=OAUTH_MCP_URL,
                headers={"Authorization": "Bearer configured-secret"},
            )
        ]
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={"sessionId": ws_session.id, "name": "weather"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "mcp": {
                "name": "weather",
                "tools": [{"name": "echo"}],
                "isUserProvided": False,
                "type": "streamable-http",
            },
        }
        assert "configured-secret" not in response.text
        assert OAUTH_MCP_URL not in response.text


class TestNamedMcpServers:
    """P1: servers come from config and are connected by name."""

    async def test_stdio_connects_by_name_from_config(
        self, client, test_config, ws_session, monkeypatch
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            StdioMcpServer(
                name="local",
                type="stdio",
                command="python -m myserver --flag",
                env={"SERVER_TOKEN": "env-secret"},
            )
        ]
        spawns = []

        @contextlib.asynccontextmanager
        async def fake_stdio(params):
            spawns.append(params)
            yield (AsyncMock(), AsyncMock())

        on_connect = AsyncMock()
        monkeypatch.setattr(test_config.code, "on_mcp_connect", on_connect)
        monkeypatch.setattr("mcp.client.stdio.stdio_client", fake_stdio)
        monkeypatch.setattr("mcp.ClientSession", FakeClientSession)
        login_as(ALICE)

        response = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "local"}
        )

        assert response.status_code == 200
        assert response.json() == {
            "success": True,
            "mcp": {
                "name": "local",
                "tools": [{"name": "echo"}],
                "isUserProvided": False,
                "type": "stdio",
            },
        }
        assert "env-secret" not in response.text

        assert len(spawns) == 1
        params = spawns[0]
        assert params.command == "python"
        assert params.args == ["-m", "myserver", "--flag"]
        assert params.env["SERVER_TOKEN"] == "env-secret"
        # The default environment is preserved, not replaced.
        assert "PATH" in params.env

        on_connect.assert_awaited_once()
        connection, session_obj = on_connect.await_args.args
        assert isinstance(connection, StdioMcpConnection)
        assert connection.name == "local"
        assert connection.command == "python"
        assert connection.args == ["-m", "myserver", "--flag"]
        assert session_obj is ws_session.mcp_sessions["local"].client

    async def test_full_command_is_rejected_before_any_spawn(
        self, client, test_config, ws_session, monkeypatch
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            StdioMcpServer(name="local", type="stdio", command="python -m myserver")
        ]
        spawns = []
        monkeypatch.setattr(
            "mcp.client.stdio.stdio_client", lambda *a, **k: spawns.append(a)
        )
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "clientType": "stdio",
                "name": "local",
                "fullCommand": "rm -rf /tmp/whatever",
            },
        )

        assert response.status_code == 422
        assert spawns == []
        assert ws_session.mcp_sessions == {}

    async def test_user_provided_sse_url_is_refused_without_outbound_request(
        self, client, test_config, ws_session, fake_sdk, monkeypatch
    ):
        test_config.features.mcp.enabled = True
        sse_calls = []
        monkeypatch.setattr(
            "mcp.client.sse.sse_client", lambda *a, **k: sse_calls.append((a, k))
        )
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "clientType": "sse",
                "name": "attacker",
                "url": "http://169.254.169.254/latest/meta-data/",
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_USER_SERVERS_DISABLED_DETAIL
        assert sse_calls == []
        assert fake_sdk.clients == []
        assert fake_sdk.transport == []

    async def test_unconfigured_name_is_rejected(self, client, test_config, ws_session):
        test_config.features.mcp.enabled = True
        login_as(ALICE)

        response = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "not-configured"}
        )

        assert response.status_code == 400
        assert "not configured" in response.json()["detail"]
        assert ws_session.mcp_sessions == {}

    async def test_sse_connects_by_name_with_configured_headers(
        self, client, test_config, ws_session, monkeypatch
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            SseMcpServer(
                name="events",
                type="sse",
                url="https://mcp.example.com/sse",
                headers={"Authorization": "Bearer configured-secret"},
            )
        ]
        sse_calls = []

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None, httpx_client_factory=None):
            sse_calls.append((url, headers, httpx_client_factory))
            yield (AsyncMock(), AsyncMock())

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse)
        monkeypatch.setattr("mcp.ClientSession", FakeClientSession)
        login_as(ALICE)

        response = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "events"}
        )

        assert response.status_code == 200
        assert response.json()["mcp"] == {
            "name": "events",
            "tools": [{"name": "echo"}],
            "isUserProvided": False,
            "type": "sse",
        }
        assert len(sse_calls) == 1
        url, headers, factory = sse_calls[0]
        assert url == "https://mcp.example.com/sse"
        assert headers == {"Authorization": "Bearer configured-secret"}
        assert factory is not None
        assert "configured-secret" not in response.text

    async def test_reconnect_replaces_the_session_atomically(
        self, client, test_config, ws_session, fake_sdk, monkeypatch
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            StreamableHttpMcpServer(
                name="weather", type="streamable-http", url=OAUTH_MCP_URL
            )
        ]
        on_disconnect = AsyncMock()
        monkeypatch.setattr(test_config.code, "on_mcp_disconnect", on_disconnect)
        login_as(ALICE)

        first = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "weather"}
        )
        assert first.status_code == 200
        first_session = ws_session.mcp_sessions["weather"]

        second = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "weather"}
        )
        assert second.status_code == 200
        second_session = ws_session.mcp_sessions["weather"]

        assert second_session is not first_session
        assert ws_session.mcp_sessions == {"weather": second_session}
        # The displaced session is torn down and its callback fired.
        assert first_session.stop_event.is_set()
        assert first_session.task.done()
        on_disconnect.assert_awaited_once_with("weather", first_session.client)

    async def test_failed_reconnect_keeps_the_working_session(
        self, client, test_config, ws_session, fake_sdk
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            StreamableHttpMcpServer(
                name="weather", type="streamable-http", url=OAUTH_MCP_URL
            )
        ]
        login_as(ALICE)

        first = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "weather"}
        )
        assert first.status_code == 200
        working = ws_session.mcp_sessions["weather"]

        async def fail_initialize(_session):
            raise RuntimeError("server is down")

        fake_sdk.session_cls.initialize_behavior = fail_initialize
        second = await client.post(
            "/mcp", json={"sessionId": ws_session.id, "name": "weather"}
        )

        assert second.status_code == 400
        assert second.json() == {"detail": MCP_CONNECT_FAILED_DETAIL}
        assert ws_session.mcp_sessions == {"weather": working}
        assert not working.stop_event.is_set()


def test_oauth_origins_are_validated_at_config_load():
    from chainlit.config import McpFeature

    for entry in (
        "https://as.example/path",
        "https://as.example?tenant=a",
        "https://as.example#fragment",
        "https://alice:secret@as.example",
        "http://10.0.0.5",
    ):
        with pytest.raises(ValidationError):
            McpFeature(oauth_authorization_origins=[entry])

    configured = McpFeature(oauth_authorization_origins=["https://as.example"])
    assert configured.oauth_authorization_origins == ["https://as.example"]


class TestUserProvidedMcpServers:
    """P2: user-provided SSE/HTTP is enabled only behind the operator allowlist."""

    def _enable_user_servers(self, test_config, allowed_urls):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.user_servers.enabled = True
        test_config.features.mcp.user_servers.allowed_urls = allowed_urls

    async def test_allowlisted_sse_connects_and_strips_restricted_headers(
        self, client, test_config, ws_session, fake_sdk, monkeypatch
    ):
        self._enable_user_servers(test_config, ["https://allowed.example.com/api"])
        sse_calls = []

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None, httpx_client_factory=None):
            sse_calls.append((url, headers, httpx_client_factory))
            yield (AsyncMock(), AsyncMock())

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse)
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": "custom",
                "url": "https://allowed.example.com/api/sse",
                "clientType": "sse",
                "headers": {
                    "Host": "evil.example.com",
                    "Cookie": "session=stolen",
                    "X-Forwarded-For": "10.0.0.1",
                    "Authorization": "Bearer user-token",
                    "X-Custom": "keep",
                },
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()["mcp"]
        assert body["isUserProvided"] is True
        assert body["url"] == "https://allowed.example.com/api/sse"
        assert body["clientType"] == "sse"
        # Headers are not echoed: the frontend persists returned headers to
        # localStorage, so a bearer token must not come back in the response.
        assert "headers" not in body
        assert "user-token" not in response.text
        assert "session=stolen" not in response.text
        assert "10.0.0.1" not in response.text

        url, headers, factory = sse_calls[0]
        assert url == "https://allowed.example.com/api/sse"
        assert headers == {
            "Authorization": "Bearer user-token",
            "X-Custom": "keep",
        }
        assert factory is not None

        http_client = factory(headers=headers)
        assert http_client.kwargs["follow_redirects"] is False
        assert len(http_client.kwargs["event_hooks"]["request"]) == 1

    async def test_non_allowlisted_user_url_is_refused_before_any_outbound_call(
        self, client, test_config, ws_session, fake_sdk, monkeypatch
    ):
        self._enable_user_servers(test_config, ["https://allowed.example.com/api"])
        sse_calls = []
        monkeypatch.setattr(
            "mcp.client.sse.sse_client", lambda *a, **k: sse_calls.append((a, k))
        )
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": "custom",
                "url": "http://169.254.169.254/latest/meta-data/",
                "clientType": "sse",
            },
        )

        assert response.status_code == 400
        assert "not in the allowed" in response.json()["detail"]
        assert sse_calls == []
        assert fake_sdk.clients == []
        assert fake_sdk.transport == []

    async def test_user_servers_disabled_is_refused_before_any_outbound_call(
        self, client, test_config, ws_session, fake_sdk, monkeypatch
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.user_servers.enabled = False
        test_config.features.mcp.user_servers.allowed_urls = [
            "https://allowed.example.com/api"
        ]
        sse_calls = []
        monkeypatch.setattr(
            "mcp.client.sse.sse_client", lambda *a, **k: sse_calls.append((a, k))
        )
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": "custom",
                "url": "https://allowed.example.com/api",
                "clientType": "sse",
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_USER_SERVERS_DISABLED_DETAIL
        assert sse_calls == []
        assert fake_sdk.clients == []

    @pytest.mark.parametrize(
        "claimed_name", ["github", "GitHub", "GITHUB", "  github  "]
    )
    async def test_user_server_cannot_alias_a_named_server(
        self, claimed_name, client, test_config, ws_session
    ):
        test_config.features.mcp.enabled = True
        test_config.features.mcp.servers = [
            SseMcpServer(name="github", type="sse", url="https://mcp.example.com/sse")
        ]
        self._enable_user_servers(test_config, ["https://evil.example.com"])
        sentinel = SimpleNamespace(name="github", close=AsyncMock())
        ws_session.mcp_sessions["github"] = sentinel
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": claimed_name,
                "url": "https://evil.example.com",
                "clientType": "sse",
            },
        )

        assert response.status_code == 400
        assert "reserved" in response.json()["detail"]
        # The rejected alias must not have evicted the real session.
        assert ws_session.mcp_sessions["github"] is sentinel

    async def test_oauth_is_gated_by_user_servers_enabled(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.user_servers.enabled = False
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_USER_SERVERS_DISABLED_DETAIL
        assert fake_sdk.clients == []
        assert fake_sdk.transport == []

    async def test_oauth_url_must_be_allowlisted(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
    ):
        oauth_config.features.mcp.user_servers.allowed_urls = [
            "https://other.example.com"
        ]
        login_as(ALICE)

        response = await client.post("/mcp", json=oauth_body(ws_session))

        assert response.status_code == 400
        assert "not in the allowed" in response.json()["detail"]
        assert fake_sdk.clients == []
        assert fake_sdk.transport == []

    async def test_hanging_initialize_times_out_and_returns_400(
        self, client, test_config, ws_session, monkeypatch
    ):
        self._enable_user_servers(test_config, ["https://allowed.example.com/api"])
        monkeypatch.setattr("chainlit.mcp._MCP_CONNECT_TIMEOUT_HTTP", 0.05)
        monkeypatch.setattr("chainlit.session._CLOSE_TIMEOUT", 0.2)

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None, httpx_client_factory=None):
            yield (AsyncMock(), AsyncMock())

        class HangingInitializeClientSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def initialize(self):
                await asyncio.Event().wait()

            async def list_tools(self):  # pragma: no cover - unreachable
                return SimpleNamespace(tools=[])

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse)
        monkeypatch.setattr("mcp.ClientSession", HangingInitializeClientSession)
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": "custom",
                "url": "https://allowed.example.com/api",
                "clientType": "sse",
            },
        )

        assert response.status_code == 400
        assert "Timed out after" in response.json()["detail"]

    async def test_blocked_destination_fails_fast_instead_of_hanging(
        self, client, test_config, ws_session, monkeypatch
    ):
        self._enable_user_servers(test_config, ["https://allowed.example.com/api"])
        monkeypatch.setattr("chainlit.session._CLOSE_TIMEOUT", 0.2)

        @contextlib.asynccontextmanager
        async def blocked_swallowing_transport(
            url, headers=None, httpx_client_factory=None
        ):
            # Mirrors the SDK: build the client, let the request hook run, and
            # swallow whatever it raises, then hang forever.
            client = httpx_client_factory()
            try:
                hook = client.event_hooks["request"][0]
                try:
                    await hook(httpx2.Request("GET", "https://evil.example.com/x"))
                except McpDestinationError:
                    pass
            finally:
                await client.aclose()
            await asyncio.Event().wait()
            yield (AsyncMock(), AsyncMock())  # pragma: no cover - unreachable

        monkeypatch.setattr("mcp.client.sse.sse_client", blocked_swallowing_transport)
        login_as(ALICE)

        start = asyncio.get_running_loop().time()
        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": "custom",
                "url": "https://allowed.example.com/api",
                "clientType": "sse",
            },
        )
        elapsed = asyncio.get_running_loop().time() - start

        assert response.status_code == 400
        assert "evil.example.com" in response.json()["detail"]
        assert elapsed < 5, f"fail-fast did not fire; took {elapsed:.1f}s"

    async def test_callback_exception_text_is_not_returned(
        self, client, test_config, ws_session, fake_sdk, monkeypatch
    ):
        self._enable_user_servers(test_config, ["https://allowed.example.com/api"])

        @contextlib.asynccontextmanager
        async def fake_sse(url, headers=None, httpx_client_factory=None):
            yield (AsyncMock(), AsyncMock())

        async def explode(_connection, _client):
            raise ValueError("secret-token")

        monkeypatch.setattr("mcp.client.sse.sse_client", fake_sse)
        monkeypatch.setattr(test_config.code, "on_mcp_connect", explode)
        login_as(ALICE)

        response = await client.post(
            "/mcp",
            json={
                "sessionId": ws_session.id,
                "name": "custom",
                "url": "https://allowed.example.com/api",
                "clientType": "sse",
            },
        )

        assert response.status_code == 400
        assert response.json()["detail"] == MCP_CONNECT_FAILED_DETAIL
        assert "secret-token" not in response.text


class TestOAuthTransportAllowlist:
    async def test_same_origin_redirect_outside_the_allowlist_fails_fast(
        self,
        client,
        oauth_config,
        ws_session,
        fake_sdk,
        fake_flows,
        mcp_pending,
        mcp_store,
        mcp_rate_limiter,
        monkeypatch,
    ):
        allowed_url = "https://mcp.example.com/api"
        oauth_config.features.mcp.user_servers.allowed_urls = [allowed_url]

        async def never(_self, timeout: float | None = None) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(
            FakeOAuthFlow, "wait_for_authorization_url", never, raising=True
        )
        storage = mcp_store.scoped("alice", canonical_server_key(allowed_url))
        await storage.set_tokens(
            OAuthToken(access_token="stored-at", refresh_token="stored-rt")
        )

        attempts: list[tuple[str, str]] = []

        @contextlib.asynccontextmanager
        async def redirecting_transport(url, http_client):
            # The SDK follows a same-origin 307/308 itself; the redirected
            # request keeps the TRANSPORT role marker.
            hook = http_client.event_hooks["request"][0]
            for target in (allowed_url, "https://mcp.example.com/admin"):
                request = httpx2.Request("POST", target)
                request.extensions[OAUTH_ROLE_EXTENSION] = TRANSPORT_ROLE
                try:
                    await hook(request)
                except McpOAuthOriginError:
                    attempts.append(("blocked", target))
                else:
                    attempts.append(("allowed", target))
            await asyncio.Event().wait()  # the SDK swallows and hangs
            yield (AsyncMock(), AsyncMock())  # pragma: no cover - unreachable

        monkeypatch.setattr(
            "mcp.client.streamable_http.streamable_http_client",
            redirecting_transport,
        )
        login_as(ALICE)

        start = asyncio.get_running_loop().time()
        response = await client.post(
            "/mcp", json=oauth_body(ws_session, url=allowed_url)
        )
        elapsed = asyncio.get_running_loop().time() - start

        assert response.status_code == 400
        assert "not in the allowed" in response.json()["detail"]
        assert attempts == [
            ("allowed", allowed_url),
            ("blocked", "https://mcp.example.com/admin"),
        ]
        assert ws_session.mcp_sessions == {}
        assert elapsed < 5, f"fail-fast did not fire; took {elapsed:.1f}s"
