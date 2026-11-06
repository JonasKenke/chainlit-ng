"""Tests for Modes system functionality."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chainlit as cl
from chainlit.emitter import ChainlitEmitter, _resolve_modes
from chainlit.mode import Mode, ModeOption
from chainlit.socket import _persist_mode_selection, change_mode
from chainlit.user import User


@pytest.fixture
def mock_modes():
    """Fixture providing sample modes for testing."""
    return [
        Mode(
            id="model",
            name="Model",
            options=[
                ModeOption(
                    id="gpt-4",
                    name="GPT-4",
                    description="Most capable model",
                    icon="sparkles",
                    default=True,
                ),
                ModeOption(
                    id="gpt-3.5-turbo",
                    name="GPT-3.5 Turbo",
                    description="Fast and efficient",
                    icon="bolt",
                    default=False,
                ),
            ],
        ),
        Mode(
            id="reasoning",
            name="Reasoning",
            options=[
                ModeOption(id="high", name="High", description="Maximum depth"),
                ModeOption(
                    id="medium", name="Medium", description="Balanced", default=True
                ),
                ModeOption(id="low", name="Low", description="Quick responses"),
            ],
        ),
    ]


@pytest.mark.asyncio
class TestModeOption:
    """Test suite for ModeOption dataclass."""

    def test_mode_option_required_fields(self):
        """Test ModeOption with required fields only."""
        option = ModeOption(id="test", name="Test Option")

        assert option.id == "test"
        assert option.name == "Test Option"
        assert option.description is None
        assert option.icon is None
        assert option.default is False

    def test_mode_option_all_fields(self):
        """Test ModeOption with all fields."""
        option = ModeOption(
            id="gpt-4",
            name="GPT-4",
            description="Most capable model",
            icon="sparkles",
            default=True,
        )

        assert option.id == "gpt-4"
        assert option.name == "GPT-4"
        assert option.description == "Most capable model"
        assert option.icon == "sparkles"
        assert option.default is True

    def test_mode_option_to_dict(self):
        """Test ModeOption serialization."""
        option = ModeOption(
            id="test",
            name="Test",
            description="Test desc",
            icon="star",
            default=True,
        )

        option_dict = option.to_dict()

        assert option_dict["id"] == "test"
        assert option_dict["name"] == "Test"
        assert option_dict["description"] == "Test desc"
        assert option_dict["icon"] == "star"
        assert option_dict["default"] is True


@pytest.mark.asyncio
class TestMode:
    """Test suite for Mode dataclass."""

    def test_mode_creation(self, mock_modes):
        """Test Mode creation with options."""
        mode = mock_modes[0]

        assert mode.id == "model"
        assert mode.name == "Model"
        assert len(mode.options) == 2

    def test_mode_to_dict(self, mock_modes):
        """Test Mode serialization."""
        mode = mock_modes[0]
        mode_dict = mode.to_dict()

        assert mode_dict["id"] == "model"
        assert mode_dict["name"] == "Model"
        assert len(mode_dict["options"]) == 2
        assert mode_dict["options"][0]["id"] == "gpt-4"

    def test_mode_default_option(self, mock_modes):
        """Test finding default option in mode."""
        mode = mock_modes[0]

        default_option = next(
            (opt for opt in mode.options if opt.default), mode.options[0]
        )

        assert default_option.id == "gpt-4"

    def test_mode_fallback_to_first(self, mock_modes):
        """Test fallback to first option when no default set."""
        mode = Mode(
            id="test",
            name="Test",
            options=[
                ModeOption(id="opt1", name="Option 1"),
                ModeOption(id="opt2", name="Option 2"),
            ],
        )

        default_option = next(
            (opt for opt in mode.options if opt.default),
            mode.options[0] if mode.options else None,
        )

        assert default_option is not None
        assert default_option.id == "opt1"

    def test_persisted_mode_value_overrides_default(self):
        """Persisted user values are restored only for opted-in modes."""
        session = SimpleNamespace(
            user=SimpleNamespace(metadata={"chainlit_modes": {"model": "slow"}}),
            mode_values={},
        )
        mode = Mode(
            id="model",
            name="Model",
            persist_to_user=True,
            options=[
                ModeOption(id="fast", name="Fast", default=True),
                ModeOption(id="slow", name="Slow"),
            ],
        )

        resolved, values = _resolve_modes(session, [mode])

        assert values == {"model": "slow"}
        assert resolved[0].options[1].default is True
        assert mode.options[0].default is True

    def test_non_persisted_mode_ignores_saved_value(self):
        """Modes remain session-only unless explicitly opted in."""
        session = SimpleNamespace(
            user=SimpleNamespace(metadata={"chainlit_modes": {"model": "slow"}}),
            mode_values={},
        )
        mode = Mode(
            id="model",
            name="Model",
            options=[
                ModeOption(id="fast", name="Fast", default=True),
                ModeOption(id="slow", name="Slow"),
            ],
        )

        _, values = _resolve_modes(session, [mode])

        assert values == {"model": "fast"}


@pytest.mark.asyncio
class TestMessageWithModes:
    """Test suite for Message with modes field."""

    async def test_message_with_modes(self, mock_chainlit_context):
        """Test that Message can be created with modes field."""
        async with mock_chainlit_context:
            modes = {"model": "gpt-4", "reasoning": "high"}
            message = cl.Message(content="Test message", modes=modes)

            assert message.modes == modes
            assert message.content == "Test message"

    async def test_message_to_dict_includes_modes(self, mock_chainlit_context):
        """Test that Message.to_dict() includes the modes field."""
        async with mock_chainlit_context:
            modes = {"model": "gpt-4", "reasoning": "medium"}
            message = cl.Message(content="Test", modes=modes)
            message_dict = message.to_dict()

            assert "modes" in message_dict
            assert message_dict["modes"] == modes

    async def test_message_from_dict_with_modes(self, mock_chainlit_context):
        """Test that Message.from_dict() correctly handles modes field."""
        async with mock_chainlit_context:
            message_dict = {
                "id": "test-id",
                "content": "Test message",
                "modes": {"model": "gpt-3.5-turbo", "reasoning": "low"},
                "type": "user_message",
                "createdAt": "2024-01-01T00:00:00",
                "output": "Test message",
            }
            message = cl.Message.from_dict(message_dict)

            assert message.modes == {"model": "gpt-3.5-turbo", "reasoning": "low"}
            assert message.content == "Test message"

    async def test_message_without_modes(self, mock_chainlit_context):
        """Test that Message works without modes field (backward compatibility)."""
        async with mock_chainlit_context:
            message = cl.Message(content="Test message")

            assert message.modes is None
            message_dict = message.to_dict()
            assert message_dict.get("modes") is None

    async def test_message_send_with_modes(self, mock_chainlit_context):
        """Test that sending a message with modes works."""
        async with mock_chainlit_context as ctx:
            modes = {"model": "gpt-4", "reasoning": "high"}
            message = cl.Message(content="Test", modes=modes)

            with patch("chainlit.message.chat_context") as mock_chat_ctx:
                with patch("chainlit.message.get_data_layer", return_value=None):
                    with patch("chainlit.message.config") as mock_config:
                        mock_config.code.author_rename = None

                        result = await message.send()

                        assert result == message
                        assert message.modes == modes
                        mock_chat_ctx.add.assert_called_once_with(message)
                        ctx.emitter.send_step.assert_called_once()

                        # Verify the dict sent to emitter includes modes
                        call_args = ctx.emitter.send_step.call_args[0][0]
                        assert call_args["modes"] == modes


@pytest.mark.asyncio
class TestEmitterSetModes:
    """Test suite for emitter set_modes functionality."""

    async def test_set_modes(
        self, mock_modes, mock_websocket_session: MagicMock
    ) -> None:
        """Test set_modes emits correct event."""
        emitter = ChainlitEmitter(mock_websocket_session)
        modes_dicts = [mode.to_dict() for mode in mock_modes]

        await emitter.set_modes(mock_modes)

        mock_websocket_session.emit.assert_called_once_with("set_modes", modes_dicts)

    async def test_set_modes_empty_list(
        self, mock_websocket_session: MagicMock
    ) -> None:
        """Test set_modes with empty list."""
        emitter = ChainlitEmitter(mock_websocket_session)

        await emitter.set_modes([])

        mock_websocket_session.emit.assert_called_once_with("set_modes", [])

    async def test_set_modes_single_mode(
        self, mock_websocket_session: MagicMock
    ) -> None:
        """Test set_modes with single mode."""
        emitter = ChainlitEmitter(mock_websocket_session)
        mode = Mode(
            id="model",
            name="Model",
            options=[ModeOption(id="gpt-4", name="GPT-4", default=True)],
        )

        await emitter.set_modes([mode])

        mock_websocket_session.emit.assert_called_once()
        call_args = mock_websocket_session.emit.call_args
        assert call_args[0][0] == "set_modes"
        assert len(call_args[0][1]) == 1
        assert call_args[0][1][0]["id"] == "model"

    async def test_change_mode_validates_and_calls_hook(self):
        session = SimpleNamespace(
            modes=[
                Mode(
                    id="model",
                    name="Model",
                    options=[
                        ModeOption(id="fast", name="Fast"),
                        ModeOption(id="slow", name="Slow"),
                    ],
                )
            ],
            mode_values={"model": "fast"},
            user=None,
            unavailable_chat_profile=None,
        )
        emit = AsyncMock()
        context = SimpleNamespace(emitter=SimpleNamespace(emit=emit))
        hook = AsyncMock()

        with (
            patch("chainlit.socket.WebsocketSession.require", return_value=session),
            patch("chainlit.socket.init_ws_context", return_value=context),
            patch("chainlit.user_session.UserSession.set"),
            patch("chainlit.socket.config.code.on_mode_update", hook),
        ):
            await change_mode("sid", {"modeId": "model", "optionId": "slow"})

        assert session.mode_values == {"model": "slow"}
        emit.assert_awaited_once_with("mode_change", {"model": "slow"})
        hook.assert_awaited_once_with({"model": "slow"})

    async def test_persist_mode_selection_merges_user_metadata(self):
        user = User(identifier="user@example.com", metadata={"other": "keep"})
        session = SimpleNamespace(user=user)
        data_layer = MagicMock()
        data_layer.get_user = AsyncMock(
            return_value=SimpleNamespace(metadata={"other": "latest"})
        )
        data_layer.create_user = AsyncMock(return_value=None)

        with patch("chainlit.socket.get_data_layer", return_value=data_layer):
            await _persist_mode_selection(session, "model", "gpt-4")

        persisted_user = data_layer.create_user.await_args.args[0]
        assert persisted_user.metadata == {
            "other": "latest",
            "chainlit_modes": {"model": "gpt-4"},
        }
        assert session.user.metadata == persisted_user.metadata


@pytest.mark.asyncio
class TestModeExports:
    """Test that Mode and ModeOption are properly exported."""

    def test_mode_exported_from_chainlit(self):
        """Test Mode is exported from chainlit package."""
        assert hasattr(cl, "Mode")
        assert cl.Mode is Mode

    def test_mode_option_exported_from_chainlit(self):
        """Test ModeOption is exported from chainlit package."""
        assert hasattr(cl, "ModeOption")
        assert cl.ModeOption is ModeOption

    def test_mode_update_hook_exported_from_chainlit(self):
        """Test the mode update hook is part of the public API."""
        assert callable(cl.on_mode_update)
