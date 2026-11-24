from unittest.mock import AsyncMock, MagicMock

import pytest

import chainlit.emitter as emitter_module
from chainlit.element import ElementDict
from chainlit.emitter import ChainlitEmitter
from chainlit.step import StepDict


@pytest.fixture
def emitter(mock_websocket_session):
    return ChainlitEmitter(mock_websocket_session)


async def test_send_element(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    element_dict: ElementDict = {
        "id": "test_element",
        "threadId": None,
        "type": "text",
        "chainlitKey": None,
        "url": None,
        "objectKey": None,
        "name": "Test Element",
        "display": "inline",
        "size": None,
        "language": None,
        "page": None,
        "props": None,
        "autoPlay": None,
        "playerConfig": None,
        "forId": None,
        "mime": None,
    }

    await emitter.send_element(element_dict)

    mock_websocket_session.emit.assert_called_once_with("element", element_dict)


async def test_send_step(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_step",
        "type": "user_message",
        "name": "Test Step",
        "output": "This is a test step",
    }

    await emitter.send_step(step_dict)

    mock_websocket_session.emit.assert_called_once_with("new_message", step_dict)


async def test_send_step_with_icon(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_step_with_icon",
        "type": "tool",
        "name": "Test Step with Icon",
        "output": "This is a test step with an icon",
        "metadata": {"icon": "search"},
    }

    await emitter.send_step(step_dict)

    mock_websocket_session.emit.assert_called_once_with("new_message", step_dict)


async def test_update_step(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_step",
        "type": "assistant_message",
        "name": "Updated Test Step",
        "output": "This is an updated test step",
    }

    await emitter.update_step(step_dict)

    mock_websocket_session.emit.assert_called_once_with("update_message", step_dict)


async def test_update_step_with_icon(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_step_with_icon",
        "type": "tool",
        "name": "Updated Test Step with Icon",
        "output": "This is an updated test step with an icon",
        "metadata": {"icon": "database"},
    }

    await emitter.update_step(step_dict)

    mock_websocket_session.emit.assert_called_once_with("update_message", step_dict)


async def test_delete_step(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_step",
        "type": "system_message",
        "name": "Deleted Test Step",
        "output": "This step will be deleted",
    }

    await emitter.delete_step(step_dict)

    mock_websocket_session.emit.assert_called_once_with("delete_message", step_dict)


async def test_send_timeout(emitter, mock_websocket_session):
    await emitter.send_timeout("ask_timeout")
    mock_websocket_session.emit.assert_called_once_with("ask_timeout", {})


async def test_clear(emitter, mock_websocket_session):
    await emitter.clear("clear_ask")
    mock_websocket_session.emit.assert_called_once_with("clear_ask", {})


async def test_send_token(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    await emitter.send_token("test_id", "test_token", is_sequence=True, is_input=False)
    mock_websocket_session.emit.assert_called_once_with(
        "stream_token",
        {"id": "test_id", "token": "test_token", "isSequence": True, "isInput": False},
    )


async def test_set_chat_settings(emitter, mock_websocket_session):
    settings = {"key": "value"}
    emitter.set_chat_settings(settings)
    assert emitter.session.chat_settings == settings


async def test_update_token_count(emitter, mock_websocket_session):
    count = 100
    await emitter.update_token_count(count)
    mock_websocket_session.emit.assert_called_once_with("token_usage", count)


async def test_task_start(emitter, mock_websocket_session):
    await emitter.task_start()
    mock_websocket_session.emit.assert_called_once_with("task_start", {})


async def test_task_end(emitter, mock_websocket_session):
    await emitter.task_end()
    mock_websocket_session.emit.assert_called_once_with("task_end", {})


async def test_stream_start(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_stream",
        "type": "run",
        "name": "Test Stream",
        "output": "This is a test stream",
    }
    await emitter.stream_start(step_dict)
    mock_websocket_session.emit.assert_called_once_with("stream_start", step_dict)


async def test_stream_start_with_icon(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    step_dict: StepDict = {
        "id": "test_stream_with_icon",
        "type": "tool",
        "name": "Test Stream with Icon",
        "output": "This is a test stream with an icon",
        "metadata": {"icon": "cpu"},
    }
    await emitter.stream_start(step_dict)
    mock_websocket_session.emit.assert_called_once_with("stream_start", step_dict)


async def test_send_toast(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    message = "This is a test message"
    await emitter.send_toast(message)
    mock_websocket_session.emit.assert_called_once_with(
        "toast", {"message": message, "type": "info"}
    )


async def test_send_toast_with_type(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    message = "This is a test message"
    await emitter.send_toast(message, type="error")
    mock_websocket_session.emit.assert_called_once_with(
        "toast", {"message": message, "type": "error"}
    )


async def test_send_toast_invalid_type(emitter: ChainlitEmitter) -> None:
    message = "This is a test message"
    with pytest.raises(ValueError, match="Invalid toast type: invalid"):
        await emitter.send_toast(message, type="invalid")  # type: ignore[arg-type]


async def test_open_side_view(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    await emitter.open_side_view("element-1")

    mock_websocket_session.emit.assert_called_once_with(
        "open_side_view", {"id": "element-1"}
    )


async def test_update_thread_title_uses_session_thread(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    mock_websocket_session.thread_id = "thread-1"

    await emitter.update_thread_title("Renamed thread")

    mock_websocket_session.emit.assert_called_once_with(
        "thread_title_updated",
        {"thread_id": "thread-1", "name": "Renamed thread"},
    )


async def test_update_thread_title_accepts_explicit_thread(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    await emitter.update_thread_title("Renamed thread", thread_id="thread-2")

    mock_websocket_session.emit.assert_called_once_with(
        "thread_title_updated",
        {"thread_id": "thread-2", "name": "Renamed thread"},
    )


async def test_refresh_threads(
    emitter: ChainlitEmitter, mock_websocket_session: MagicMock
) -> None:
    await emitter.refresh_threads()

    mock_websocket_session.emit.assert_called_once_with("thread_list_updated", {})


async def test_module_refresh_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_context = MagicMock()
    mock_context.emitter.refresh_threads = AsyncMock()
    monkeypatch.setattr(emitter_module, "context", mock_context)

    await emitter_module.refresh_threads()

    mock_context.emitter.refresh_threads.assert_awaited_once_with()


async def test_module_update_thread_title(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_context = MagicMock()
    mock_context.emitter.update_thread_title = AsyncMock()
    monkeypatch.setattr(emitter_module, "context", mock_context)

    await emitter_module.update_thread_title("Renamed", thread_id="thread-2")

    mock_context.emitter.update_thread_title.assert_awaited_once_with(
        "Renamed", thread_id="thread-2"
    )


async def test_init_thread_refreshes_thread_list(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.thread_id = "test_thread_id"
    mock_websocket_session.user = None
    mock_websocket_session.chat_profile = None
    mock_websocket_session.flush_method_queue = AsyncMock()
    monkeypatch.setattr(emitter_module, "get_data_layer", lambda: AsyncMock())

    await emitter.init_thread("Hello")

    mock_websocket_session.emit.assert_any_call(
        "first_interaction",
        {"interaction": "Hello", "thread_id": "test_thread_id"},
    )
    mock_websocket_session.emit.assert_any_call("thread_list_updated", {})


async def test_init_thread_skips_refresh_without_data_layer(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.thread_id = "test_thread_id"
    monkeypatch.setattr(emitter_module, "get_data_layer", lambda: None)

    await emitter.init_thread("Hello")

    emitted = [call.args[0] for call in mock_websocket_session.emit.call_args_list]
    assert "first_interaction" in emitted
    assert "thread_list_updated" not in emitted


def _user(identifier: str) -> MagicMock:
    user = MagicMock()
    user.identifier = identifier
    return user


def _session(session_id: str, identifier: str | None) -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.emit = AsyncMock()
    session.user = _user(identifier) if identifier else None
    return session


def _patch_user_sessions(
    monkeypatch: pytest.MonkeyPatch, sessions: list[MagicMock]
) -> None:
    monkeypatch.setattr(
        "chainlit.session.WebsocketSession.sessions_for_user",
        classmethod(lambda cls, identifier: sessions),
    )


async def test_update_thread_title_broadcasts_to_user_sessions(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.id = "session-1"
    mock_websocket_session.thread_id = "thread-1"
    mock_websocket_session.user = _user("alice")
    other = _session("session-2", "alice")
    _patch_user_sessions(monkeypatch, [other, mock_websocket_session])

    await emitter.update_thread_title("Renamed thread")

    expected = {"thread_id": "thread-1", "name": "Renamed thread"}
    other.emit.assert_awaited_once_with("thread_title_updated", expected)
    mock_websocket_session.emit.assert_awaited_once_with(
        "thread_title_updated", expected
    )


async def test_refresh_threads_broadcasts_to_user_sessions(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.id = "session-1"
    mock_websocket_session.user = _user("alice")
    other = _session("session-2", "alice")
    _patch_user_sessions(monkeypatch, [other, mock_websocket_session])

    await emitter.refresh_threads()

    other.emit.assert_awaited_once_with("thread_list_updated", {})
    mock_websocket_session.emit.assert_awaited_once_with("thread_list_updated", {})


async def test_thread_events_stay_session_scoped_without_user(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.id = "session-1"
    mock_websocket_session.thread_id = "thread-1"
    mock_websocket_session.user = None
    other = _session("session-2", "alice")
    _patch_user_sessions(monkeypatch, [other])

    await emitter.update_thread_title("Renamed thread")
    await emitter.refresh_threads()

    other.emit.assert_not_awaited()
    mock_websocket_session.emit.assert_any_await(
        "thread_title_updated",
        {"thread_id": "thread-1", "name": "Renamed thread"},
    )
    mock_websocket_session.emit.assert_any_await("thread_list_updated", {})


async def test_thread_events_do_not_duplicate_the_origin_session(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.id = "session-1"
    mock_websocket_session.thread_id = "thread-1"
    mock_websocket_session.user = _user("alice")
    _patch_user_sessions(monkeypatch, [mock_websocket_session])

    await emitter.update_thread_title("Renamed thread")

    mock_websocket_session.emit.assert_awaited_once_with(
        "thread_title_updated",
        {"thread_id": "thread-1", "name": "Renamed thread"},
    )


async def test_thread_broadcast_survives_a_failing_session(
    emitter: ChainlitEmitter,
    mock_websocket_session: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_websocket_session.id = "session-1"
    mock_websocket_session.thread_id = "thread-1"
    mock_websocket_session.user = _user("alice")
    broken = _session("session-2", "alice")
    broken.emit = AsyncMock(side_effect=RuntimeError("socket is gone"))
    _patch_user_sessions(monkeypatch, [broken, mock_websocket_session])

    await emitter.update_thread_title("Renamed thread")

    broken.emit.assert_awaited_once()
    mock_websocket_session.emit.assert_awaited_once_with(
        "thread_title_updated",
        {"thread_id": "thread-1", "name": "Renamed thread"},
    )
