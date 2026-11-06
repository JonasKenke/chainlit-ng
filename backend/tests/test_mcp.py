import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError
from starlette.requests import Request

from chainlit.auth import create_jwt, get_current_user
from chainlit.config import config
from chainlit.mcp import (
    HttpMcpConnection,
    SseMcpConnection,
    StdioMcpConnection,
    validate_mcp_command,
)
from chainlit.mcp_oauth import (
    MCP_OAUTH_DETAILS,
    McpOAuthTokenStore,
    PendingAuthorizations,
)
from chainlit.server import (
    MCP_CONNECT_FAILED_DETAIL,
    MCP_OAUTH_CALLBACK_PATH,
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


class TestValidateMcpCommand:
    """Test suite for validate_mcp_command function."""

    def test_validate_simple_command(self):
        """Test validation of a simple command."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python", "node"]

            env, executable, args = validate_mcp_command("python -m mcp_server")

            assert env == {}
            assert executable == "python"
            assert args == ["-m", "mcp_server"]

    def test_validate_command_with_path(self):
        """Test validation of command with full path."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            env, executable, args = validate_mcp_command(
                "/usr/bin/python -m mcp_server"
            )

            assert env == {}
            assert executable == "/usr/bin/python"
            assert args == ["-m", "mcp_server"]

    def test_validate_command_with_windows_path(self):
        """Test validation of command with Windows path (using forward slashes or quoted)."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python.exe"]

            # Use forward slashes which work on Windows and don't get escaped by shlex
            env, executable, args = validate_mcp_command(
                "C:/Python/python.exe -m mcp_server"
            )

            assert env == {}
            assert executable == "C:/Python/python.exe"
            assert args == ["-m", "mcp_server"]

    def test_validate_command_with_env_vars(self):
        """Test validation of command with environment variables."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["node"]

            env, executable, args = validate_mcp_command(
                "MY_VAR=value NODE_ENV=production node server.js"
            )

            assert env == {"MY_VAR": "value", "NODE_ENV": "production"}
            assert executable == "node"
            assert args == ["server.js"]

    def test_validate_command_with_env_var_with_spaces(self):
        """Test validation of command with env var containing spaces."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            env, executable, args = validate_mcp_command(
                'MY_VAR="value with spaces" python script.py'
            )

            assert env == {"MY_VAR": "value with spaces"}
            assert executable == "python"
            assert args == ["script.py"]

    def test_validate_command_with_quoted_args(self):
        """Test validation of command with quoted arguments."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            env, executable, args = validate_mcp_command(
                'python script.py --arg "value with spaces"'
            )

            assert env == {}
            assert executable == "python"
            assert args == ["script.py", "--arg", "value with spaces"]

    def test_validate_command_with_multiple_args(self):
        """Test validation of command with multiple arguments."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["node"]

            env, executable, args = validate_mcp_command(
                "node server.js --port 3000 --host localhost"
            )

            assert env == {}
            assert executable == "node"
            assert args == ["server.js", "--port", "3000", "--host", "localhost"]

    def test_validate_command_not_in_allowed_list(self):
        """Test that validation fails for disallowed executable."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python", "node"]

            with pytest.raises(ValueError, match="Only commands in"):
                validate_mcp_command("bash script.sh")

    def test_validate_empty_command(self):
        """Test that validation fails for empty command."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            with pytest.raises(ValueError, match="Empty command string"):
                validate_mcp_command("")

    def test_validate_command_with_invalid_syntax(self):
        """Test that validation fails for invalid command syntax."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            with pytest.raises(ValueError, match="Invalid command string"):
                validate_mcp_command('python "unclosed quote')

    def test_validate_command_with_none_allowed_executables(self):
        """Test validation when allowed_executables is None (all allowed)."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = None

            env, executable, args = validate_mcp_command("any_command --arg value")

            assert env == {}
            assert executable == "any_command"
            assert args == ["--arg", "value"]

    def test_validate_command_with_invalid_env_var_format(self):
        """Test that validation fails for invalid env var format."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            with pytest.raises(ValueError, match="Invalid environment variable format"):
                validate_mcp_command("INVALID_ENV python script.py")

    def test_validate_command_with_complex_env_vars(self):
        """Test validation with complex environment variables."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            env, executable, args = validate_mcp_command(
                'API_KEY=sk-123456 BASE_URL="https://api.example.com" python app.py'
            )

            assert env == {
                "API_KEY": "sk-123456",
                "BASE_URL": "https://api.example.com",
            }
            assert executable == "python"
            assert args == ["app.py"]

    def test_validate_command_with_equals_in_arg(self):
        """Test validation with equals sign in argument."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            env, executable, args = validate_mcp_command(
                "python script.py --config=value"
            )

            assert env == {}
            assert executable == "python"
            assert args == ["script.py", "--config=value"]

    def test_validate_command_preserves_arg_order(self):
        """Test that argument order is preserved."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["node"]

            _, _, args = validate_mcp_command(
                "node app.js arg1 arg2 arg3 --flag1 --flag2"
            )

            assert args == ["app.js", "arg1", "arg2", "arg3", "--flag1", "--flag2"]


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

    def test_validate_command_with_special_characters_in_path(self):
        """Test validation with special characters in path."""
        mcp_module = sys.modules["chainlit.mcp"]
        with patch.object(mcp_module, "config") as mock_config:
            mock_config.features.mcp.stdio.allowed_executables = ["python"]

            _, executable, args = validate_mcp_command(
                "/opt/my-app/bin/python script.py"
            )

            assert executable == "/opt/my-app/bin/python"
            assert args == ["script.py"]


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
        test_config.features.mcp.streamable_http.enabled = True
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
                json={
                    "sessionId": ws_session.id,
                    "clientType": "streamable-http",
                    "name": "weather",
                    "url": SERVER_KEY,
                },
            )

        assert response.status_code == 400
        assert response.json() == {"detail": MCP_CONNECT_FAILED_DETAIL}
        assert hostile not in response.text
        assert "hostile-value" in caplog.text
