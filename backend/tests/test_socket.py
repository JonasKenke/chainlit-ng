import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

import chainlit.approval as approval_module
from chainlit.approval import ApprovalPending
from chainlit.config import config
from chainlit.session import WebsocketSession
from chainlit.socket import (
    _authenticate_connection,
    _get_token,
    _get_token_from_cookie,
    audio_end,
    clean_session,
    connect,
    connection_successful,
    edit_message,
    load_user_env,
    message,
    persist_user_session,
    process_message,
    restore_existing_session,
    resume_thread,
    stop,
    window_message,
)
from chainlit.types import ChatProfile


class TestGetTokenFromCookie:
    """Test suite for _get_token_from_cookie function."""

    def test_get_token_from_cookie_with_valid_cookie(self):
        """Test extracting token from valid cookie header."""
        with patch("chainlit.socket.get_token_from_cookies") as mock_get_token:
            mock_get_token.return_value = "test_token"
            environ = {"HTTP_COOKIE": "session=abc123; token=test_token"}

            result = _get_token_from_cookie(environ)

            assert result == "test_token"
            mock_get_token.assert_called_once()

    def test_get_token_from_cookie_without_cookie(self):
        """Test when no cookie header is present."""
        environ = {}
        result = _get_token_from_cookie(environ)
        assert result is None

    def test_get_token_from_cookie_with_empty_cookie(self):
        """Test with empty cookie header."""
        with patch("chainlit.socket.get_token_from_cookies") as mock_get_token:
            mock_get_token.return_value = None
            environ = {"HTTP_COOKIE": ""}

            result = _get_token_from_cookie(environ)

            assert result is None


class TestGetToken:
    """Test suite for _get_token function."""

    def test_get_token_calls_get_token_from_cookie(self):
        """Test that _get_token delegates to _get_token_from_cookie."""
        with patch("chainlit.socket._get_token_from_cookie") as mock_get_cookie:
            mock_get_cookie.return_value = "token_value"
            environ = {"HTTP_COOKIE": "token=token_value"}

            result = _get_token(environ)

            assert result == "token_value"
            mock_get_cookie.assert_called_once_with(environ)


class TestAuthenticateConnection:
    """Test suite for _authenticate_connection function."""

    @pytest.mark.asyncio
    async def test_authenticate_connection_with_valid_token(self):
        """Test authentication with valid token."""
        mock_user = Mock()
        mock_user.identifier = "user123"

        with patch("chainlit.socket._get_token") as mock_get_token:
            with patch("chainlit.socket.get_current_user") as mock_get_user:
                mock_get_token.return_value = "valid_token"
                mock_get_user.return_value = mock_user

                environ = {"HTTP_COOKIE": "token=valid_token"}
                user, token = await _authenticate_connection(environ)

                assert user == mock_user
                assert token == "valid_token"
                mock_get_user.assert_called_once_with(token="valid_token")

    @pytest.mark.asyncio
    async def test_authenticate_connection_without_token(self):
        """Test authentication without token."""
        with patch("chainlit.socket._get_token") as mock_get_token:
            mock_get_token.return_value = None

            environ = {}
            user, token = await _authenticate_connection(environ)

            assert user is None
            assert token is None

    @pytest.mark.asyncio
    async def test_authenticate_connection_with_invalid_token(self):
        """Test authentication with invalid token."""
        with patch("chainlit.socket._get_token") as mock_get_token:
            with patch("chainlit.socket.get_current_user") as mock_get_user:
                mock_get_token.return_value = "invalid_token"
                mock_get_user.return_value = None

                environ = {"HTTP_COOKIE": "token=invalid_token"}
                user, token = await _authenticate_connection(environ)

                assert user is None
                assert token is None


class TestRestoreExistingSession:
    """Test suite for restore_existing_session function."""

    def test_restore_existing_session_success(self):
        """Test restoring an existing session."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = None
        emit_fn = Mock()
        emit_call_fn = Mock()
        environ = {"HTTP_COOKIE": "token=token"}

        with patch.object(WebsocketSession, "get_by_id") as mock_get:
            mock_get.return_value = mock_session

            result = restore_existing_session(
                "new_sid", "session_123", emit_fn, emit_call_fn, environ
            )

            assert result is True
            mock_session.restore.assert_called_once_with(new_socket_id="new_sid")
            assert mock_session.emit == emit_fn
            assert mock_session.emit_call == emit_call_fn
            assert mock_session.environ == environ

    def test_restore_existing_session_with_matching_user(self):
        """Test restoring a session for its authenticated owner."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        authenticated_user = Mock(identifier="user123")

        with patch.object(WebsocketSession, "get_by_id") as mock_get:
            mock_get.return_value = mock_session

            result = restore_existing_session(
                "new_sid",
                "session_123",
                Mock(),
                Mock(),
                {"HTTP_COOKIE": "token=token"},
                user=authenticated_user,
            )

            assert result is True
            mock_session.restore.assert_called_once_with(new_socket_id="new_sid")

    def test_restore_existing_session_rejects_different_user(self):
        """Test that a session cannot be restored by another user."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="victim")
        authenticated_user = Mock(identifier="attacker")

        with patch.object(WebsocketSession, "get_by_id") as mock_get:
            mock_get.return_value = mock_session

            with pytest.raises(ConnectionRefusedError, match="authorization failed"):
                restore_existing_session(
                    "new_sid",
                    "session_123",
                    Mock(),
                    Mock(),
                    {"HTTP_COOKIE": "token=token"},
                    user=authenticated_user,
                )

            mock_session.restore.assert_not_called()

    def test_restore_existing_session_not_found(self):
        """Test when session is not found."""
        with patch.object(WebsocketSession, "get_by_id") as mock_get:
            mock_get.return_value = None

            result = restore_existing_session(
                "new_sid", "session_123", Mock(), Mock(), {"HTTP_COOKIE": "token=token"}
            )

            assert result is False


class TestPersistUserSession:
    """Test suite for persist_user_session function."""

    @pytest.mark.asyncio
    async def test_persist_user_session_with_data_layer(self):
        """Test persisting user session with data layer."""
        mock_data_layer = AsyncMock()

        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = mock_data_layer

            metadata = {"key": "value"}
            await persist_user_session("thread_123", metadata)

            mock_data_layer.update_thread.assert_called_once_with(
                thread_id="thread_123", metadata=metadata
            )

    @pytest.mark.asyncio
    async def test_persist_user_session_without_data_layer(self):
        """Test persisting when no data layer is available."""
        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = None

            # Should not raise an error
            await persist_user_session("thread_123", {"key": "value"})


class TestResumeThread:
    """Test suite for resume_thread function."""

    @pytest.mark.asyncio
    async def test_resume_thread_without_data_layer(self):
        """Test resume thread when no data layer exists."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock()
        mock_session.thread_id_to_resume = "thread_123"

        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = None

            result = await resume_thread(mock_session)

            assert result is None

    @pytest.mark.asyncio
    async def test_resume_thread_without_user(self):
        """Test resume thread when session has no user."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = None
        mock_session.thread_id_to_resume = "thread_123"

        result = await resume_thread(mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_resume_thread_without_thread_id(self):
        """Test resume thread when no thread_id_to_resume."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock()
        mock_session.thread_id_to_resume = None

        result = await resume_thread(mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_resume_thread_thread_not_found(self):
        """Test resume thread when thread doesn't exist."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        mock_session.thread_id_to_resume = "thread_123"
        mock_session.id = "session_123"

        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = None

        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = mock_data_layer

            result = await resume_thread(mock_session)

            assert result is None
            mock_data_layer.get_thread.assert_called_once_with(thread_id="thread_123")

    @pytest.mark.asyncio
    async def test_resume_thread_user_not_author(self):
        """Test resume thread when user is not the thread author."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        mock_session.thread_id_to_resume = "thread_123"
        mock_session.id = "session_123"

        thread = {"userIdentifier": "different_user", "metadata": {}}
        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = thread

        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = mock_data_layer

            result = await resume_thread(mock_session)

            assert result is None

    @pytest.mark.asyncio
    async def test_resume_thread_rejects_an_ownerless_row(self):
        """An existing row with no owner is rejected, not guessed."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        mock_session.thread_id_to_resume = "thread_123"
        mock_session.id = "session_123"

        thread = {"userIdentifier": None, "metadata": {"is_shared": True}}
        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = thread

        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = mock_data_layer

            result = await resume_thread(mock_session)

            assert result is None

    @pytest.mark.asyncio
    async def test_resume_thread_success(self, monkeypatch):
        """Test successful thread resumption."""
        from chainlit.user_session import user_sessions

        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        mock_session.thread_id_to_resume = "thread_123"
        mock_session.id = "session_123"
        mock_session.language = "en-US"
        monkeypatch.setattr(
            "chainlit.socket.config.code.set_chat_profiles",
            AsyncMock(
                return_value=[ChatProfile(name="gpt-4", markdown_description="GPT")]
            ),
        )

        metadata = {
            "chat_profile": "gpt-4",
            "chat_settings": {"temperature": 0.7},
        }
        thread = {"userIdentifier": "user123", "metadata": metadata}

        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = thread

        original_sessions = user_sessions.copy()
        try:
            with patch("chainlit.socket.get_data_layer") as mock_get_dl:
                mock_get_dl.return_value = mock_data_layer

                result = await resume_thread(mock_session)

                assert result == thread
                assert mock_session.chat_profile == "gpt-4"
                assert mock_session.chat_settings == {"temperature": 0.7}
                assert user_sessions.get("session_123") == metadata
        finally:
            user_sessions.clear()
            user_sessions.update(original_sessions)

    @pytest.mark.asyncio
    async def test_resume_thread_with_string_metadata(self, monkeypatch):
        """Test thread resumption with JSON string metadata."""
        from chainlit.user_session import user_sessions

        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        mock_session.thread_id_to_resume = "thread_123"
        mock_session.id = "session_123"
        mock_session.language = "en-US"
        monkeypatch.setattr(
            "chainlit.socket.config.code.set_chat_profiles",
            AsyncMock(
                return_value=[ChatProfile(name="gpt-4", markdown_description="GPT")]
            ),
        )

        metadata_dict = {"chat_profile": "gpt-4"}
        thread = {
            "id": "thread-123",
            "userIdentifier": "user123",
            "metadata": json.dumps(metadata_dict),
        }

        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = thread

        original_sessions = user_sessions.copy()
        try:
            with patch("chainlit.socket.get_data_layer") as mock_get_dl:
                mock_get_dl.return_value = mock_data_layer

                result = await resume_thread(mock_session)

                assert result == thread
                assert mock_session.chat_profile == "gpt-4"
        finally:
            user_sessions.clear()
            user_sessions.update(original_sessions)


class TestLoadUserEnv:
    """Test suite for load_user_env function."""

    def test_load_user_env_with_valid_json(self):
        """Test loading valid user environment JSON."""
        user_env = '{"API_KEY": "secret", "ENV_VAR": "value"}'

        with patch("chainlit.socket.config") as mock_config:
            mock_config.project.user_env = []

            result = load_user_env(user_env)

            assert result == {"API_KEY": "secret", "ENV_VAR": "value"}

    def test_load_user_env_with_required_keys(self):
        """Test loading user env with required keys."""
        user_env = '{"API_KEY": "secret", "OTHER_KEY": "value"}'

        with patch("chainlit.socket.config") as mock_config:
            mock_config.project.user_env = ["API_KEY", "OTHER_KEY"]

            result = load_user_env(user_env)

            assert result == {"API_KEY": "secret", "OTHER_KEY": "value"}

    def test_load_user_env_missing_required_key(self):
        """Test error when required key is missing."""
        user_env = '{"API_KEY": "secret"}'

        with patch("chainlit.socket.config") as mock_config:
            mock_config.project.user_env = ["API_KEY", "MISSING_KEY"]

            with pytest.raises(
                ConnectionRefusedError, match="Missing user environment variable"
            ):
                load_user_env(user_env)

    def test_load_user_env_none_with_required_keys(self):
        """Test error when user_env is None but keys are required."""
        with patch("chainlit.socket.config") as mock_config:
            mock_config.project.user_env = ["API_KEY"]

            with pytest.raises(
                ConnectionRefusedError, match="Missing user environment variables"
            ):
                load_user_env(None)

    def test_load_user_env_none_without_required_keys(self):
        """Test when user_env is None and no keys are required."""
        with patch("chainlit.socket.config") as mock_config:
            mock_config.project.user_env = []

            result = load_user_env(None)

            assert result == {}


class TestCleanSession:
    """Test suite for clean_session function."""

    @pytest.mark.asyncio
    async def test_clean_session_with_existing_session(self):
        """Test marking session for cleanup."""
        mock_session = Mock(spec=WebsocketSession)
        mock_session.to_clear = False

        with patch.object(WebsocketSession, "get") as mock_get:
            mock_get.return_value = mock_session

            await clean_session("socket_123")

            assert mock_session.to_clear is True
            mock_get.assert_called_once_with("socket_123")

    @pytest.mark.asyncio
    async def test_clean_session_without_session(self):
        """Test clean_session when session doesn't exist."""
        with patch.object(WebsocketSession, "get") as mock_get:
            mock_get.return_value = None

            # Should not raise an error
            await clean_session("socket_123")


class TestSocketEdgeCases:
    """Test suite for socket edge cases."""

    def test_restore_existing_session_with_none_session_id(self):
        """Test restore with None session_id."""
        with patch.object(WebsocketSession, "get_by_id") as mock_get:
            mock_get.return_value = None

            result = restore_existing_session(None, None, Mock(), Mock(), None)

            assert result is False

    @pytest.mark.asyncio
    async def test_persist_user_session_with_empty_metadata(self):
        """Test persisting empty metadata."""
        mock_data_layer = AsyncMock()

        with patch("chainlit.socket.get_data_layer") as mock_get_dl:
            mock_get_dl.return_value = mock_data_layer

            await persist_user_session("thread_123", {})

            mock_data_layer.update_thread.assert_called_once_with(
                thread_id="thread_123", metadata={}
            )

    def test_load_user_env_with_empty_json(self):
        """Test loading empty user environment."""
        user_env = "{}"

        with patch("chainlit.socket.config") as mock_config:
            mock_config.project.user_env = []

            result = load_user_env(user_env)

            assert result == {}

    @pytest.mark.asyncio
    async def test_resume_thread_with_empty_metadata(self):
        """Test resuming thread with empty metadata."""
        from chainlit.user_session import user_sessions

        mock_session = Mock(spec=WebsocketSession)
        mock_session.user = Mock(identifier="user123")
        mock_session.thread_id_to_resume = "thread_123"
        mock_session.id = "session_123"

        thread = {"userIdentifier": "user123", "metadata": {}}

        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = thread

        original_sessions = user_sessions.copy()
        try:
            with patch("chainlit.socket.get_data_layer") as mock_get_dl:
                mock_get_dl.return_value = mock_data_layer

                result = await resume_thread(mock_session)

                assert result == thread
                assert user_sessions.get("session_123") == {}
        finally:
            user_sessions.clear()
            user_sessions.update(original_sessions)

    @pytest.mark.asyncio
    async def test_authenticate_connection_with_exception(self):
        """Test authentication when get_current_user raises exception."""
        with patch("chainlit.socket._get_token") as mock_get_token:
            with patch("chainlit.socket.get_current_user") as mock_get_user:
                mock_get_token.return_value = "token"
                mock_get_user.side_effect = Exception("Auth error")

                environ = {"HTTP_COOKIE": "token=token"}

                # Should propagate the exception
                with pytest.raises(Exception, match="Auth error"):
                    await _authenticate_connection(environ)


class TestConnectionSuccessfulIdempotency:
    """Regression tests: on_chat_start must fire exactly once per
    WebsocketSession, even when connection_successful is dispatched multiple
    times (Socket.IO reconnect, React StrictMode double-mount, reverse-proxy
    WS 101 retry).

    Refs chainlit#2535, chainlit#2549, chainlit#2228.
    """

    @pytest.fixture(autouse=True)
    def socket_is_connected(self):
        # Sessions in these unit tests represent connected Socket.IO clients.
        with patch(
            "chainlit.socket.sio.manager.is_connected", return_value=True
        ) as connected:
            yield connected

    @pytest.mark.asyncio
    async def test_on_chat_resume_not_duplicated_on_reconnect(
        self, mock_session_factory
    ):
        """A live session reconnect must not reload its persisted thread."""
        session = mock_session_factory(has_first_interaction=False)
        session.socket_id = "sid-1"
        session.restored = False
        session.thread_id_to_resume = "thread-1"
        session.chat_started = False

        mock_context = Mock()
        mock_context.session = session
        mock_context.emitter = AsyncMock()

        mock_config = Mock()
        mock_config.code.on_chat_start = None
        mock_config.code.on_chat_resume = AsyncMock()
        thread = {"id": "thread-1", "steps": [{"type": "user_message"}]}

        with (
            patch("chainlit.socket.init_ws_context", return_value=mock_context),
            patch("chainlit.socket.config", mock_config),
            patch(
                "chainlit.socket.resume_thread", new_callable=AsyncMock
            ) as load_thread,
            patch("chainlit.socket.Message.from_dict") as from_dict,
            patch("chainlit.socket.chat_context.add") as add_to_chat_context,
        ):
            load_thread.return_value = thread
            await connection_successful("sid-1")
            session.restored = True
            await connection_successful("sid-2")

        load_thread.assert_awaited_once_with(session)
        mock_config.code.on_chat_resume.assert_awaited_once_with(thread)
        mock_context.emitter.resume_thread.assert_awaited_once_with(thread)
        from_dict.assert_called_once_with(thread["steps"][0])
        add_to_chat_context.assert_called_once_with(from_dict.return_value)

    @pytest.mark.asyncio
    async def test_on_chat_start_not_duplicated_on_reconnect(
        self, mock_session_factory
    ):
        """Reconnect path (session.restored=True): on_chat_start scheduled once."""
        on_chat_start = AsyncMock()

        session = mock_session_factory(has_first_interaction=False)
        session.restored = True
        session.chat_started = False
        session.current_task = None
        session.thread_id_to_resume = None

        mock_context = Mock()
        mock_context.session = session
        mock_context.emitter = AsyncMock()

        mock_config = Mock()
        mock_config.code.on_chat_start = on_chat_start
        mock_config.code.on_chat_resume = None

        with (
            patch("chainlit.socket.init_ws_context", return_value=mock_context),
            patch("chainlit.socket.config", mock_config),
        ):
            await connection_successful("sid-1")
            # Simulate reconnect: same session object, chat_started now True.
            await connection_successful("sid-1")

        assert on_chat_start.call_count == 1, (
            "on_chat_start must be scheduled exactly once per WebsocketSession"
        )
        assert session.chat_started is True

    @pytest.mark.asyncio
    async def test_on_chat_start_fires_once_on_fresh_session(
        self, mock_session_factory
    ):
        """Normal one-connect path still greets exactly once."""
        on_chat_start = AsyncMock()

        session = mock_session_factory(has_first_interaction=False)
        session.restored = False
        session.chat_started = False
        session.current_task = None
        session.thread_id_to_resume = None

        mock_context = Mock()
        mock_context.session = session
        mock_context.emitter = AsyncMock()

        mock_config = Mock()
        mock_config.code.on_chat_start = on_chat_start
        mock_config.code.on_chat_resume = None

        with (
            patch("chainlit.socket.init_ws_context", return_value=mock_context),
            patch("chainlit.socket.config", mock_config),
        ):
            await connection_successful("sid-1")

        assert on_chat_start.call_count == 1
        assert session.chat_started is True

    @pytest.mark.asyncio
    async def test_on_chat_start_not_duplicated_on_fresh_then_reconnect(
        self, mock_session_factory
    ):
        """Fresh connect followed by a reconnect still fires exactly once."""
        on_chat_start = AsyncMock()

        session = mock_session_factory(has_first_interaction=False)
        session.restored = False
        session.chat_started = False
        session.current_task = None
        session.thread_id_to_resume = None

        mock_context = Mock()
        mock_context.session = session
        mock_context.emitter = AsyncMock()

        mock_config = Mock()
        mock_config.code.on_chat_start = on_chat_start
        mock_config.code.on_chat_resume = None

        with (
            patch("chainlit.socket.init_ws_context", return_value=mock_context),
            patch("chainlit.socket.config", mock_config),
        ):
            await connection_successful("sid-1")
            session.restored = True
            await connection_successful("sid-1")

        assert on_chat_start.call_count == 1


class TestConnectSessionIdValidation:
    @pytest.mark.parametrize(
        "session_id",
        ["../../etc", "/tmp/x", "a/b", "..", "", None, 123, "a" * 129],
    )
    async def test_connect_refuses_unsafe_session_id(self, session_id):
        auth = {"sessionId": session_id, "clientType": "webapp"}

        with patch("chainlit.socket.WebsocketSession") as mock_session:
            with pytest.raises(ConnectionRefusedError):
                await connect("sid", {}, auth)

        mock_session.assert_not_called()


class TestConnectThreadIdValidation:
    @pytest.mark.parametrize(
        "thread_id",
        [
            "../../etc/passwd",
            "x' OR '1'='1",
            "00000000-0000-0000-0000-00000000000",
            "00000000-0000-0000-0000-00000000000g",
            "a" * 36,
            123,
        ],
    )
    async def test_connect_refuses_invalid_thread_id(self, thread_id):
        auth = {
            "sessionId": "valid-session-id",
            "clientType": "webapp",
            "threadId": thread_id,
        }

        with patch("chainlit.socket.WebsocketSession") as mock_session:
            with pytest.raises(ConnectionRefusedError, match="invalid thread id"):
                await connect("sid", {}, auth)

        mock_session.assert_not_called()

    @pytest.mark.parametrize(
        "thread_id", [None, "", "0190a4b0-2f3c-4d5e-8f90-123456789abc"]
    )
    async def test_connect_accepts_uuid_or_empty_thread_id(self, thread_id):
        auth = {
            "sessionId": "valid-session-id",
            "clientType": "webapp",
            "threadId": thread_id,
        }

        with (
            patch("chainlit.socket.require_login", return_value=False),
            patch("chainlit.socket.WebsocketSession") as mock_session,
        ):
            mock_session.get_by_id.return_value = None
            assert await connect("sid", {}, auth) is True

        mock_session.assert_called_once()


@pytest.fixture(autouse=True)
def clean_approval_registry():
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()
    approval_module._deleted_threads.clear()
    approval_module._managed_thread_users.clear()
    yield
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()
    approval_module._deleted_threads.clear()
    approval_module._managed_thread_users.clear()


class TestApprovalEntryGuard:
    @pytest.fixture
    def context(self):
        mock_context = Mock()
        mock_context.emitter = AsyncMock()
        return mock_context

    @pytest.fixture(autouse=True)
    def bind_session(self, mock_session, context):
        mock_session.unavailable_chat_profile = None
        mock_session.current_task = None
        mock_session.thread_id = "thread-1"
        with (
            patch(
                "chainlit.socket.WebsocketSession.require", return_value=mock_session
            ),
            patch("chainlit.socket.init_ws_context", return_value=context),
            patch(
                "chainlit.socket.guard_pending_approval",
                side_effect=ApprovalPending("pending"),
            ),
        ):
            yield context

    async def test_client_message_is_refused(self, context, mock_session, caplog):
        on_message = AsyncMock()
        with patch.object(config.code, "on_message", on_message):
            await message("sid", {"message": {"output": "hi"}})

        assert mock_session.current_task is None
        on_message.assert_not_awaited()
        context.emitter.process_message.assert_not_awaited()
        context.emitter.send_toast.assert_not_awaited()
        assert "Refused a new run" in caplog.text

    async def test_repeated_messages_do_not_retoast(self, context, mock_session):
        on_message = AsyncMock()
        with patch.object(config.code, "on_message", on_message):
            for _ in range(3):
                await message("sid", {"message": {"output": "hi"}})

        on_message.assert_not_awaited()
        context.emitter.process_message.assert_not_awaited()
        context.emitter.send_toast.assert_not_awaited()

    async def test_edit_message_is_refused_before_chat_context_changes(self):
        on_message = AsyncMock()
        with (
            patch.object(config.code, "on_message", on_message),
            patch("chainlit.socket.chat_context") as chat_context,
        ):
            await edit_message("sid", {"message": {"id": "m1", "output": "edited"}})

        on_message.assert_not_awaited()
        chat_context.get.assert_not_called()
        chat_context.delete_session.assert_not_called()

    async def test_window_message_is_refused(self):
        on_window_message = AsyncMock()
        with patch.object(config.code, "on_window_message", on_window_message):
            await window_message("sid", {"data": 1})

        on_window_message.assert_not_awaited()

    async def test_audio_end_is_refused_before_first_interaction(self, mock_session):
        on_audio_end = AsyncMock()
        mock_session.has_first_interaction = False
        with patch.object(config.code, "on_audio_end", on_audio_end):
            await audio_end("sid")

        on_audio_end.assert_not_awaited()
        assert mock_session.has_first_interaction is False


class TestRunOwnership:
    @pytest.fixture
    def context(self):
        mock_context = Mock()
        mock_context.emitter = AsyncMock()
        return mock_context

    async def test_client_message_binds_current_task(self, mock_session, context):
        started = asyncio.Event()

        async def run(session, payload):
            started.set()
            await asyncio.Future()

        mock_session.unavailable_chat_profile = None
        mock_session.current_task = None
        with (
            patch(
                "chainlit.socket.WebsocketSession.require", return_value=mock_session
            ),
            patch("chainlit.socket.init_ws_context", return_value=context),
            patch("chainlit.socket.guard_pending_approval"),
            patch("chainlit.socket.process_message", run),
        ):
            await message("sid", {"message": {"output": "hi"}})
            await asyncio.wait_for(started.wait(), timeout=1)

            assert mock_session.current_task is not None
            mock_session.current_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mock_session.current_task

    async def test_context_init_error_is_not_masked(self, mock_session):
        mock_session.unavailable_chat_profile = None
        with patch("chainlit.socket.init_ws_context", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                await process_message(mock_session, {"message": {"output": "hi"}})

    async def test_edit_message_binds_current_task(self, mock_session, context):
        started = asyncio.Event()

        async def run(session, payload):
            started.set()
            await asyncio.Future()

        mock_session.unavailable_chat_profile = None
        mock_session.current_task = None
        with (
            patch(
                "chainlit.socket.WebsocketSession.require", return_value=mock_session
            ),
            patch("chainlit.socket.init_ws_context", return_value=context),
            patch("chainlit.socket.guard_pending_approval"),
            patch("chainlit.socket.process_edit_message", run),
        ):
            await edit_message("sid", {"message": {"id": "m1", "output": "edited"}})
            await asyncio.wait_for(started.wait(), timeout=1)

            assert mock_session.current_task is not None
            mock_session.current_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await mock_session.current_task

    async def test_stop_supersedes_before_cancelling(self, mock_session, context):
        order = []

        async def supersede(thread_id, reason="deleted"):
            order.append(("supersede", thread_id, reason))

        run_task = asyncio.create_task(asyncio.sleep(60))
        run_task.add_done_callback(lambda _: order.append(("cancelled",)))
        mock_session.unavailable_chat_profile = None
        mock_session.current_task = run_task
        with (
            patch("chainlit.socket.WebsocketSession.get", return_value=mock_session),
            patch("chainlit.socket.init_ws_context", return_value=context),
            patch("chainlit.socket.supersede_thread_approvals", supersede),
            patch("chainlit.socket.Message") as message_cls,
        ):
            message_cls.return_value.send = AsyncMock()
            await stop("sid")
            with pytest.raises(asyncio.CancelledError):
                await run_task

        assert order[0] == ("supersede", "test_thread_id", "stopped")
        assert ("cancelled",) in order


class TestApprovalReconnect:
    async def test_reconnect_does_not_reemit_an_approval(self, mock_session_factory):
        session = mock_session_factory(has_first_interaction=True)
        session.socket_id = "sid-1"
        session.restored = True
        session.thread_id_to_resume = None

        mock_context = Mock()
        mock_context.session = session
        mock_context.emitter = AsyncMock()

        mock_config = Mock()
        mock_config.code.on_chat_start = None
        mock_config.code.on_chat_resume = None

        with (
            patch("chainlit.socket.init_ws_context", return_value=mock_context),
            patch("chainlit.socket.config", mock_config),
        ):
            await connection_successful("sid-1")

        mock_context.emitter.send_approval.assert_not_awaited()


class TestResumeOrphan:
    async def test_resume_marks_a_persisted_pending_record_orphaned(self):
        session = Mock(spec=WebsocketSession)
        session.user = Mock(identifier="user123")
        session.thread_id_to_resume = "thread-123"
        session.id = "session-123"
        session.generation = "generation-new"
        session.language = "en-US"

        thread = {
            "id": "thread-123",
            "userIdentifier": "user123",
            "metadata": {},
            "steps": [
                {
                    "id": "step-1",
                    "metadata": {
                        "approval": {
                            "id": "approval-1",
                            "revision": 1,
                            "status": "pending",
                        }
                    },
                }
            ],
        }
        mock_data_layer = AsyncMock()
        mock_data_layer.get_thread.return_value = thread
        mock_data_layer.write_approval.return_value = True

        from chainlit.user_session import user_sessions

        original_sessions = user_sessions.copy()
        try:
            with (
                patch("chainlit.socket.get_data_layer", return_value=mock_data_layer),
                patch("chainlit.approval.get_data_layer", return_value=mock_data_layer),
            ):
                result = await resume_thread(session)
        finally:
            user_sessions.clear()
            user_sessions.update(original_sessions)

        assert result["steps"][0]["metadata"]["approval"]["status"] == "orphaned"
        kwargs = mock_data_layer.write_approval.call_args.kwargs
        assert kwargs["expected_revision"] == 1
        assert kwargs["record"]["terminalReason"] == "server_restart"
