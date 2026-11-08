"""Negative-path coverage for user-controlled input surfaces.

Covers validation, authentication and error handling for threads, file
uploads, custom elements, actions, prompt-gallery CRUD and Socket.IO
``client_message`` payloads. Tests marked ``xfail(strict=True)`` pin a
confirmed defect from ``docs/research/input-negative-paths.md`` (KRE-187
finding IDs); they turn into failures when the defect is fixed so the marker
can be removed.

No production code is changed by this module.
"""

import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

import chainlit.data as data_mod
import chainlit.socket as socket
from chainlit.auth import get_current_user
from chainlit.config import SpontaneousFileUploadFeature, config
from chainlit.context import ChainlitContext, context_var
from chainlit.data.base import BaseDataLayer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.emitter import ChainlitEmitter
from chainlit.server import app, upload_file
from chainlit.session import WebsocketSession, ws_sessions_id, ws_sessions_sid
from chainlit.types import AskFileSpec, PageInfo, PaginatedResponse
from chainlit.user import PersistedUser

USER = PersistedUser(
    id="user-1",
    identifier="user@example.com",
    createdAt="2024-01-01T00:00:00Z",
)
OTHER_USER = PersistedUser(
    id="user-2",
    identifier="other@example.com",
    createdAt="2024-01-01T00:00:00Z",
)


def _client(current_user):
    app.dependency_overrides[get_current_user] = lambda: current_user
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def api():
    client = _client(USER)
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def anon_api():
    client = _client(None)
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def layer(monkeypatch):
    """Install a mock data layer for both server routes and data-layer ACLs."""
    data_layer = AsyncMock(spec=BaseDataLayer)
    monkeypatch.setattr(data_mod, "_data_layer", data_layer)
    monkeypatch.setattr(data_mod, "_data_layer_initialized", True)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)
    return data_layer


@pytest.fixture
def no_data_layer(monkeypatch):
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)


@pytest.fixture
def session(monkeypatch):
    """A session owned by USER, resolvable by ``session_id == session.id``."""
    session = Mock(spec=WebsocketSession)
    session.id = "sess-1"
    session.thread_id = "owner-thread"
    session.user = USER
    session.files = {}
    session.files_spec = {}
    session.uploaded_file_counts = {}
    session.files_dir = Mock()
    session.persist_file = AsyncMock(
        return_value={"id": "file-1", "name": "a", "type": "text/plain", "size": 1}
    )
    session.emit = AsyncMock()
    session.emit_call = AsyncMock()
    session.unavailable_chat_profile = None
    session.has_first_interaction = True
    session.current_task = None
    session.get_config = Mock(
        return_value=SimpleNamespace(
            code=SimpleNamespace(action_callbacks={}),
        )
    )

    def get_by_id(session_id):
        return session if session_id == session.id else None

    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(get_by_id))
    return session


@pytest.fixture
async def ws(monkeypatch, mock_chainlit_context):
    """A real ChainlitEmitter bound to a mock WebSocket session."""
    async with mock_chainlit_context as base:
        session = base.session
        session.id = "ws-session"
        session.thread_id = "ws-thread"
        session.files = {}
        session.files_spec = {}
        session.emit = AsyncMock()
        session.emit_call = AsyncMock()
        session.unavailable_chat_profile = None
        session.has_first_interaction = True
        session.current_task = None

        emitter = ChainlitEmitter(session)
        ctx = ChainlitContext(session, emitter=emitter)
        token = context_var.set(ctx)

        monkeypatch.setattr(socket, "init_ws_context", lambda _: ctx)
        monkeypatch.setattr(socket, "get_data_layer", lambda: None)
        monkeypatch.setattr("chainlit.message.get_data_layer", lambda: None)
        monkeypatch.setattr("chainlit.data.get_data_layer", lambda: None)
        monkeypatch.setattr(config.code, "author_rename", None)
        on_message = AsyncMock()
        monkeypatch.setattr(config.code, "on_message", on_message)

        try:
            yield session, emitter, on_message
        finally:
            context_var.reset(token)


def _message_payload(**overrides):
    payload = {
        "id": "3b0f7f7e-2f1f-4a7b-9c5e-9f1f2a3b4c5d",
        "type": "user_message",
        "output": "hello",
        "createdAt": "2024-01-01T00:00:00Z",
        "name": "User",
    }
    payload.update(overrides)
    return {"message": payload}


def _emitted_errors(emitter):
    return [
        call.args
        for call in emitter.session.emit.await_args_list
        if len(call.args) >= 2 and call.args[0] == "new_message"
    ]


# ---------------------------------------------------------------------------
# Thread endpoints: authentication, data layer, missing resources
# ---------------------------------------------------------------------------

THREAD_ROUTES = [
    ("get", "/project/thread/t1", None),
    ("put", "/project/thread", {"threadId": "t1", "name": "x"}),
    ("put", "/project/thread/share", {"threadId": "t1", "isShared": True}),
    ("delete", "/project/thread", {"threadId": "t1"}),
    ("get", "/project/thread/t1/element/e1", None),
]


def _call(api, method, path, body):
    if method == "delete":
        return api.request("DELETE", path, json=body)
    return api.request(method.upper(), path, json=body)


@pytest.mark.parametrize(("method", "path", "body"), THREAD_ROUTES)
def test_thread_endpoints_require_authentication(anon_api, layer, method, path, body):
    layer.get_thread_author.return_value = USER.identifier
    response = _call(anon_api, method, path, body)
    assert response.status_code == 401


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [route for route in THREAD_ROUTES if route[1].startswith("/project/thread")],
)
def test_thread_endpoints_require_data_layer(api, no_data_layer, method, path, body):
    response = _call(api, method, path, body)
    assert response.status_code == 400


@pytest.mark.parametrize(("method", "path", "body"), THREAD_ROUTES)
def test_missing_thread_returns_404(api, layer, method, path, body):
    layer.get_thread_author.side_effect = ValueError("Author not found")
    response = _call(api, method, path, body)
    assert response.status_code == 404, response.text


def test_unexpected_thread_author_error_returns_500(api, layer):
    layer.get_thread_author.side_effect = RuntimeError("database unavailable")
    response = api.get("/project/thread/t1")
    assert response.status_code == 500, response.text


def test_get_thread_rejects_non_author(api, layer):
    layer.get_thread_author.return_value = OTHER_USER.identifier
    response = api.get("/project/thread/t1")
    assert response.status_code == 401


def test_get_element_rejects_non_author(api, layer):
    layer.get_thread_author.return_value = OTHER_USER.identifier
    response = api.get("/project/thread/t1/element/e1")
    assert response.status_code == 401


def test_get_missing_element_returns_404(api, layer):
    layer.get_thread_author.return_value = USER.identifier
    layer.get_element.return_value = None
    response = api.get("/project/thread/t1/element/nope")
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("name", ["", "   ", " \n\t "])
def test_rename_thread_rejects_blank_name(api, layer, name):
    layer.get_thread_author.return_value = USER.identifier
    response = api.put("/project/thread", json={"threadId": "t1", "name": name})
    assert response.status_code == 422, response.text
    layer.update_thread.assert_not_awaited()


def test_rename_thread_accepts_valid_name_unchanged(api, layer):
    layer.get_thread_author.return_value = USER.identifier
    response = api.put("/project/thread", json={"threadId": "t1", "name": "  padded  "})
    assert response.status_code == 200, response.text
    layer.update_thread.assert_awaited_once_with("t1", name="  padded  ")


def test_rename_thread_missing_fields_returns_422(api, layer):
    response = api.put("/project/thread", json={"threadId": "t1"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /project/threads: pagination and filter ownership
# ---------------------------------------------------------------------------


def _thread_page(count):
    thread = {
        "id": "t1",
        "createdAt": "2024-01-01T00:00:00Z",
        "name": "n",
        "userId": USER.id,
        "userIdentifier": USER.identifier,
        "tags": [],
        "metadata": {},
        "steps": [{"output": "hello"}],
        "elements": [],
    }
    return [dict(thread) for _ in range(count)]


@pytest.fixture
def sql_layer(monkeypatch):
    layer = SQLAlchemyDataLayer(conninfo="sqlite+aiosqlite://")
    monkeypatch.setattr(
        layer, "get_all_user_threads", AsyncMock(return_value=_thread_page(5))
    )
    monkeypatch.setattr(data_mod, "_data_layer", layer)
    monkeypatch.setattr(data_mod, "_data_layer_initialized", True)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: layer)
    return layer


@pytest.mark.parametrize("first", [0, -1])
def test_threads_non_positive_page_size_returns_422(api, sql_layer, first):
    response = api.post(
        "/project/threads",
        json={"pagination": {"first": first}, "filter": {}},
    )
    assert response.status_code == 422, (
        f"observed {response.status_code} for first={first} with "
        f"{len(response.json().get('data', []))} threads"
    )


def test_threads_page_size_one_returns_first_thread(api, sql_layer):
    response = api.post(
        "/project/threads",
        json={"pagination": {"first": 1}, "filter": {}},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 1
    assert body["pageInfo"]["hasNextPage"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"filter": {}},
        {"pagination": {}, "filter": {}},
        {"pagination": {"first": "not-an-int"}, "filter": {}},
        {"pagination": {"first": 1}, "filter": {"feedback": 2}},
    ],
)
def test_threads_malformed_pagination_returns_422(api, layer, body):
    response = api.post("/project/threads", json=body)
    assert response.status_code == 422


def test_threads_filter_user_id_is_forced_to_current_user(api, layer):
    layer.list_threads.return_value = PaginatedResponse(
        pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
        data=[],
    )
    response = api.post(
        "/project/threads",
        json={
            "pagination": {"first": 10},
            "filter": {"userId": OTHER_USER.id},
        },
    )
    assert response.status_code == 200
    received_filter = layer.list_threads.await_args.args[1]
    assert received_filter.userId == USER.id


# ---------------------------------------------------------------------------
# /project/share: missing and unshared threads are not disclosed
# ---------------------------------------------------------------------------


def test_shared_thread_missing_returns_404(anon_api, layer):
    layer.get_thread.return_value = None
    response = anon_api.get("/project/share/t1")
    assert response.status_code == 404


def test_shared_thread_unshared_returns_404(anon_api, layer):
    layer.get_thread.return_value = {
        "id": "t1",
        "userIdentifier": USER.identifier,
        "metadata": {"is_shared": False},
        "steps": [],
        "elements": [],
    }
    response = anon_api.get("/project/share/t1")
    assert response.status_code == 404


def test_shared_thread_is_public(anon_api, layer):
    layer.get_thread.return_value = {
        "id": "t1",
        "userIdentifier": USER.identifier,
        "metadata": {"is_shared": True, "chat_profile": "private"},
        "steps": [],
        "elements": [],
    }
    response = anon_api.get("/project/share/t1")
    assert response.status_code == 200
    assert response.json()["metadata"] == {"is_shared": True}


# ---------------------------------------------------------------------------
# POST /project/file: session ownership, content type, zero-byte files
# ---------------------------------------------------------------------------


def test_upload_rejects_unknown_session(api, monkeypatch):
    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(lambda _: None))
    response = api.post(
        "/project/file",
        files={"file": ("a.txt", b"x", "text/plain")},
        params={"session_id": "does-not-exist"},
    )
    assert response.status_code == 404


def test_upload_rejects_other_users_session(api, session):
    session.user = OTHER_USER
    response = api.post(
        "/project/file",
        files={"file": ("a.txt", b"x", "text/plain")},
        params={"session_id": session.id},
    )
    assert response.status_code == 401


def test_upload_accepts_zero_byte_file(api, session):
    response = api.post(
        "/project/file",
        files={"file": ("empty.txt", b"", "text/plain")},
        params={"session_id": session.id},
    )
    assert response.status_code == 200
    assert session.persist_file.await_args.kwargs["content"] == b""


async def test_upload_without_filename_returns_400(session):
    file = UploadFile(
        file=io.BytesIO(b"x"),
        filename="",
        headers=Headers({"content-type": "text/plain"}),
    )

    with pytest.raises(HTTPException) as exc_info:
        await upload_file(USER, session.id, file)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "No filename for uploaded file"
    assert not session.persist_file.called


def test_upload_without_content_type_returns_400(api, session):
    response = api.post(
        "/project/file",
        files={"file": ("a.txt", b"x", "")},
        params={"session_id": session.id},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "No content type for uploaded file"
    assert not session.persist_file.called


def test_upload_enforces_ask_max_files(api, session):
    session.files_spec = {
        ask_id: AskFileSpec(
            type="file",
            step_id=ask_id,
            timeout=90,
            accept=["text/plain"],
            max_files=1,
            max_size_mb=2,
        )
        for ask_id in ("ask-1", "ask-2")
    }

    def upload(ask_parent_id, name):
        return api.post(
            "/project/file",
            files={"file": (name, b"x", "text/plain")},
            params={"session_id": session.id, "ask_parent_id": ask_parent_id},
        )

    assert upload("ask-1", "first.txt").status_code == 200
    over_limit = upload("ask-1", "second.txt")
    assert over_limit.status_code == 400, over_limit.text
    assert over_limit.json()["detail"] == "File upload limit reached"

    # The limit is per ask, not per session.
    assert upload("ask-2", "other-ask.txt").status_code == 200
    assert session.persist_file.await_count == 2


def test_upload_persist_failure_does_not_consume_limit(api, session):
    session.files_spec = {
        "ask-1": AskFileSpec(
            type="file",
            step_id="ask-1",
            timeout=90,
            accept=["text/plain"],
            max_files=1,
            max_size_mb=2,
        )
    }
    session.persist_file.side_effect = RuntimeError("disk full")

    failed = api.post(
        "/project/file",
        files={"file": ("first.txt", b"x", "text/plain")},
        params={"session_id": session.id, "ask_parent_id": "ask-1"},
    )
    assert failed.status_code == 500

    session.persist_file.side_effect = None
    retried = api.post(
        "/project/file",
        files={"file": ("retry.txt", b"x", "text/plain")},
        params={"session_id": session.id, "ask_parent_id": "ask-1"},
    )
    assert retried.status_code == 200, retried.text


def test_upload_enforces_spontaneous_max_files(api, session, monkeypatch):
    monkeypatch.setattr(
        config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(enabled=True, accept=["text/plain"], max_files=1),
    )

    def upload(name, mime):
        return api.post(
            "/project/file",
            files={"file": (name, b"x", mime)},
            params={"session_id": session.id},
        )

    # A rejected MIME type must not consume the upload budget.
    assert upload("image.png", "image/png").status_code == 400
    assert upload("first.txt", "text/plain").status_code == 200

    over_limit = upload("second.txt", "text/plain")
    assert over_limit.status_code == 400, over_limit.text
    assert over_limit.json()["detail"] == "File upload limit reached"
    assert session.persist_file.await_count == 1


def test_upload_unknown_ask_parent_returns_404(api, session):
    response = api.post(
        "/project/file",
        files={"file": ("a.txt", b"x", "text/plain")},
        params={"session_id": session.id, "ask_parent_id": "missing"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# /project/action: session, payload shape, unknown callback
# ---------------------------------------------------------------------------


def test_action_unknown_session_returns_404(api, monkeypatch):
    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(lambda _: None))
    response = api.post(
        "/project/action",
        json={"sessionId": "missing", "action": {"name": "x", "payload": {}}},
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize(
    "action",
    [
        {"payload": {}},
        {"name": "x"},
        {"name": 1, "payload": {}},
        {"name": "x", "payload": "not-a-dict"},
    ],
)
def test_action_malformed_action_returns_422(api, session, action):
    response = api.post(
        "/project/action", json={"sessionId": session.id, "action": action}
    )
    assert response.status_code == 422, response.text


def test_action_unknown_callback_returns_404(api, session):
    response = api.post(
        "/project/action",
        json={"sessionId": session.id, "action": {"name": "nope", "payload": {}}},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# /project/element: session, payload shape, thread ownership
# ---------------------------------------------------------------------------


def _custom_element(**overrides):
    element = {
        "type": "custom",
        "id": "element-1",
        "threadId": "owner-thread",
        "name": "widget",
        "display": "inline",
        "props": {"value": 1},
    }
    element.update(overrides)
    return element


def test_element_update_unknown_session_returns_404(api, monkeypatch):
    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(lambda _: None))
    response = api.put(
        "/project/element",
        json={"sessionId": "missing", "element": _custom_element()},
    )
    assert response.status_code == 404, response.text


def test_element_delete_unknown_session_returns_404(api, monkeypatch):
    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(lambda _: None))
    response = api.request(
        "DELETE",
        "/project/element",
        json={"sessionId": "missing", "element": _custom_element()},
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize(
    "element",
    [
        {},
        {"type": "custom"},
        {"type": "custom", "id": "e1", "name": "n"},
        {**_custom_element(), "props": "not-a-dict"},
    ],
)
def test_element_malformed_payload_returns_422(api, session, method, element):
    response = api.request(
        method.upper(),
        "/project/element",
        json={"sessionId": session.id, "element": element},
    )
    assert response.status_code == 422, response.text


def test_element_update_rejects_non_custom_type(api, session):
    response = api.put(
        "/project/element",
        json={
            "sessionId": session.id,
            "element": {**_custom_element(), "type": "text"},
        },
    )
    assert response.status_code == 200
    assert response.json() == {"success": False}


@pytest.mark.xfail(
    strict=True,
    reason="KRE-187-F4: custom-element writes use the caller-supplied threadId.",
)
def test_element_update_does_not_trust_client_thread_id(api, session, layer):
    response = api.put(
        "/project/element",
        json={
            "sessionId": session.id,
            "element": _custom_element(threadId="victim-thread"),
        },
    )
    assert response.status_code == 200

    for _ in range(200):
        if layer.create_element.await_count:
            break
        time.sleep(0.01)
    assert layer.create_element.await_count == 1, "element was not persisted"
    persisted = layer.create_element.await_args.args[0]
    assert persisted.thread_id == session.thread_id, (
        f"persisted thread_id={persisted.thread_id!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="KRE-187-F4: custom-element deletes use the caller-supplied threadId.",
)
def test_element_delete_does_not_trust_client_thread_id(api, session, layer):
    response = api.request(
        "DELETE",
        "/project/element",
        json={
            "sessionId": session.id,
            "element": _custom_element(id="victim-element", threadId="victim-thread"),
        },
    )
    assert response.status_code == 200
    element_id, thread_id = layer.delete_element.await_args.args
    assert thread_id == session.thread_id, f"deleted ({element_id!r}, {thread_id!r})"


# ---------------------------------------------------------------------------
# Prompt gallery: required fields and blank values
# ---------------------------------------------------------------------------


@pytest.fixture
def gallery(monkeypatch, layer):
    monkeypatch.setattr(config.features, "prompt_gallery", True)
    prompt = {
        "id": "p1",
        "title": "t",
        "content": "c",
        "userId": USER.id,
        "isShared": False,
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
    }
    layer.create_prompt.return_value = prompt
    layer.get_prompt.return_value = prompt
    layer.update_prompt.side_effect = lambda updated: updated
    return layer


def test_create_prompt_requires_title_and_content(api, gallery):
    response = api.post("/project/prompts", json={})
    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "content": "real content"},
        {"title": "   ", "content": "real content"},
        {"title": "real title", "content": ""},
        {"title": "real title", "content": " \n\t "},
    ],
)
def test_create_prompt_rejects_blank_fields(api, gallery, payload):
    response = api.post("/project/prompts", json=payload)
    assert response.status_code == 422, response.text
    gallery.create_prompt.assert_not_awaited()


def test_create_prompt_stores_values_unchanged(api, gallery):
    response = api.post("/project/prompts", json={"title": "  t  ", "content": "  c  "})
    assert response.status_code == 200, response.text
    prompt = gallery.create_prompt.await_args.args[0]
    assert (prompt["title"], prompt["content"]) == ("  t  ", "  c  ")


@pytest.mark.parametrize("field", ["title", "content"])
@pytest.mark.parametrize("blank", ["", "   ", " \n\t "])
def test_update_prompt_rejects_blank_fields(api, gallery, field, blank):
    response = api.put("/project/prompts/p1", json={field: blank})
    assert response.status_code == 422, response.text
    gallery.update_prompt.assert_not_awaited()


def test_update_prompt_stores_value_unchanged(api, gallery):
    response = api.put("/project/prompts/p1", json={"title": "  padded  "})
    assert response.status_code == 200, response.text
    updated = gallery.update_prompt.await_args.args[0]
    assert updated["title"] == "  padded  "


# ---------------------------------------------------------------------------
# Socket.IO client_message: payload handling and content limits
# ---------------------------------------------------------------------------


async def test_socket_empty_and_whitespace_messages_are_accepted(ws):
    session, _emitter, on_message = ws

    await socket.process_message(session, _message_payload(output=""))
    assert on_message.await_count == 1
    assert on_message.await_args.args[0].content == ""

    await socket.process_message(session, _message_payload(output="   \n\t"))
    assert on_message.await_count == 2
    assert on_message.await_args.args[0].content == "   \n\t"


async def test_socket_oversized_message_is_accepted(ws):
    session, _emitter, on_message = ws
    content = "x" * 1_000_000

    await socket.process_message(session, _message_payload(output=content))

    assert on_message.await_count == 1
    assert len(on_message.await_args.args[0].content) == len(content)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"message": None},
        {"message": {"id": "not-a-uuid"}},
        {
            "message": {
                "id": "3b0f7f7e-2f1f-4a7b-9c5e-9f1f2a3b4c5d",
                "output": "missing createdAt",
            }
        },
    ],
)
async def test_socket_malformed_message_emits_error_without_calling_app(ws, payload):
    session, emitter, on_message = ws

    await socket.process_message(session, payload)

    assert on_message.await_count == 0
    errors = _emitted_errors(emitter)
    assert errors, "expected an error message to be emitted to the client"
    assert errors[-1][1]["isError"] is True


def test_unknown_chat_profile_falls_back_to_global_config(monkeypatch):
    monkeypatch.setattr(
        config.code,
        "set_chat_profiles",
        AsyncMock(return_value=[]),
    )
    session = WebsocketSession(
        id="profile-session",
        socket_id="profile-socket",
        emit=Mock(),
        emit_call=Mock(),
        user_env={},
        client_type="webapp",
        environ={},
        chat_profile="does-not-exist",
    )
    try:
        assert session.chat_profile == "does-not-exist"
        assert session.get_config() is config
    finally:
        ws_sessions_id.pop("profile-session", None)
        ws_sessions_sid.pop("profile-socket", None)
