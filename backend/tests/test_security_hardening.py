"""Regression tests for the 2026-11 security audit fixes.

Covers CORS credentials, Engine.IO origin checks, auth CSRF, feedback
ownership without thread grants, error redaction, upload streaming, and
storage-safe element disposition/keys.
"""

import asyncio
import io
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.data.base import BaseDataLayer
from chainlit.element import Element, File as FileElement, Image
from chainlit.server import (
    _cors_allow_credentials,
    _socket_origin_allowed,
    app,
)
from chainlit.session import WebsocketSession
from chainlit.types import FeedbackThreadLookup
from chainlit.user import User

ALICE = User(identifier="alice")
BOB = User(identifier="bob")


@pytest.fixture
def client():
    from chainlit.auth import get_current_user_optional

    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_user_optional, None)
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_current_user_optional, None)


def as_user(user):
    app.dependency_overrides[get_current_user] = lambda: user


# ---------------------------------------------------------------------------
# CORS and Engine.IO origin policy
# ---------------------------------------------------------------------------


def _cors_app(origins, credentials: bool) -> TestClient:
    app_under_test = FastAPI()

    @app_under_test.get("/x")
    def x():
        return {"ok": True}

    app_under_test.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return TestClient(app_under_test)


def test_wildcard_origin_does_not_receive_credentials(monkeypatch):
    monkeypatch.setattr(config.project, "allow_origins", ["*"])
    monkeypatch.setattr(config.project, "allow_credentials", None)

    assert _cors_allow_credentials() is False

    response = _cors_app(["*"], _cors_allow_credentials()).get(
        "/x", headers={"Origin": "https://evil.example"}
    )
    assert response.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_explicit_origin_keep_credentials(monkeypatch):
    monkeypatch.setattr(config.project, "allow_origins", ["https://app.example"])
    monkeypatch.setattr(config.project, "allow_credentials", None)

    assert _cors_allow_credentials() is True

    response = _cors_app(["https://app.example"], _cors_allow_credentials()).get(
        "/x", headers={"Origin": "https://app.example"}
    )
    assert response.headers["access-control-allow-origin"] == "https://app.example"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_wildcard_with_explicit_opt_in_keeps_legacy_behavior(monkeypatch):
    monkeypatch.setattr(config.project, "allow_origins", ["*"])
    monkeypatch.setattr(config.project, "allow_credentials", True)

    assert _cors_allow_credentials() is True


def test_socket_origin_same_origin_and_explicit_allowlist(monkeypatch):
    environ = {"HTTP_HOST": "app.example"}
    monkeypatch.setattr(config.project, "allow_origins", ["*"])
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)

    assert _socket_origin_allowed(None, environ) is True
    assert _socket_origin_allowed("https://app.example", environ) is True
    assert _socket_origin_allowed("https://evil.example", environ) is False

    monkeypatch.setattr(config.project, "allow_origins", ["https://embed.example"])
    assert _socket_origin_allowed("https://embed.example", environ) is True
    assert _socket_origin_allowed("https://evil.example", environ) is False


def test_socket_wildcard_is_open_only_without_login(monkeypatch):
    environ = {"HTTP_HOST": "app.example"}
    monkeypatch.setattr(config.project, "allow_origins", ["*"])
    monkeypatch.setattr("chainlit.server.require_login", lambda: False)

    assert _socket_origin_allowed("https://evil.example", environ) is True

    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    assert _socket_origin_allowed("https://evil.example", environ) is False
    assert _socket_origin_allowed("null", environ) is False


# ---------------------------------------------------------------------------
# Auth CSRF and sticky-session input
# ---------------------------------------------------------------------------


def test_login_rejects_cross_origin_and_does_not_call_callback(client, monkeypatch):
    callback = AsyncMock(return_value=ALICE)
    monkeypatch.setattr(config.code, "password_auth_callback", callback)

    response = client.post(
        "/login",
        data={"username": "alice", "password": "pw"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    callback.assert_not_awaited()


def test_login_allows_same_origin(client, monkeypatch):
    monkeypatch.setattr(
        config.code, "password_auth_callback", AsyncMock(return_value=ALICE)
    )
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "x" * 32)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)

    response = client.post(
        "/login",
        data={"username": "alice", "password": "pw"},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200, response.text
    assert "access_token" in response.cookies


def test_logout_rejects_cross_origin(client):
    response = client.post("/logout", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_jwt_login_rejects_cross_origin_before_reading_token(client):
    response = client.post("/auth/jwt", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_set_session_cookie_validates_id_and_origin(client):
    cross_origin = client.post(
        "/set-session-cookie",
        json={"session_id": "abc"},
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403

    invalid = client.post("/set-session-cookie", json={"session_id": "../x"})
    assert invalid.status_code == 400

    valid = client.post("/set-session-cookie", json={"session_id": "abc"})
    assert valid.status_code == 200
    assert valid.cookies.get("X-Chainlit-Session-id") == "abc"


# ---------------------------------------------------------------------------
# Feedback ownership without the thread-grants flag
# ---------------------------------------------------------------------------


@pytest.fixture
def feedback_layer():
    layer = AsyncMock(spec=BaseDataLayer)
    layer.feedback_authorization_supported = True
    layer.thread_grants_supported = True
    with (
        patch("chainlit.server.get_data_layer", return_value=layer),
        patch("chainlit.data.acl.get_data_layer", return_value=layer),
    ):
        yield layer


def _ws_session(user, thread_id="thread-1"):
    session = Mock(spec=WebsocketSession)
    session.id = f"{user.identifier}-session"
    session.user = user
    session.thread_id = thread_id
    session.generation = "gen"
    session.has_first_interaction = True
    return session


def _feedback_payload():
    return {
        "sessionId": "alice-session",
        "feedback": {"forId": "step-1", "value": 1, "threadId": "thread-1"},
    }


def test_feedback_update_rejects_foreign_thread_without_grants(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    as_user(ALICE)
    feedback_layer.get_thread_author.return_value = BOB.identifier

    with patch(
        "chainlit.session.WebsocketSession.get_by_id",
        return_value=_ws_session(ALICE),
    ):
        response = client.put("/feedback", json=_feedback_payload())

    assert response.status_code == 404
    feedback_layer.upsert_feedback.assert_not_awaited()


def test_feedback_update_allows_owner_without_grants(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    as_user(ALICE)
    feedback_layer.get_thread_author.return_value = ALICE.identifier
    feedback_layer.step_belongs_to_thread.return_value = True
    feedback_layer.get_feedback_thread_id.return_value = FeedbackThreadLookup(
        status="not_found"
    )
    feedback_layer.upsert_feedback.return_value = "feedback-1"

    with patch(
        "chainlit.session.WebsocketSession.get_by_id",
        return_value=_ws_session(ALICE),
    ):
        response = client.put("/feedback", json=_feedback_payload())

    assert response.status_code == 200, response.text
    feedback_layer.upsert_feedback.assert_awaited_once()


def test_feedback_delete_of_foreign_row_is_noop_without_grants(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    as_user(BOB)
    feedback_layer.get_feedback_thread_id.return_value = FeedbackThreadLookup(
        status="found", thread_id="thread-1"
    )
    feedback_layer.get_thread_author.return_value = ALICE.identifier

    response = client.request("DELETE", "/feedback", json={"feedbackId": "feedback-1"})

    assert response.status_code == 200
    assert response.json() == {"success": True}
    feedback_layer.delete_feedback.assert_not_awaited()


def test_feedback_persistence_error_is_redacted(client, feedback_layer, monkeypatch):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    as_user(ALICE)
    feedback_layer.get_thread_author.return_value = ALICE.identifier
    feedback_layer.step_belongs_to_thread.return_value = True
    feedback_layer.get_feedback_thread_id.return_value = FeedbackThreadLookup(
        status="not_found"
    )
    feedback_layer.upsert_feedback.side_effect = RuntimeError("secret SQL detail")

    with patch(
        "chainlit.session.WebsocketSession.get_by_id",
        return_value=_ws_session(ALICE),
    ):
        response = client.put("/feedback", json=_feedback_payload())

    assert response.status_code == 500
    assert "secret SQL detail" not in response.text
    assert response.json()["detail"] == "Failed to update feedback"


# ---------------------------------------------------------------------------
# Element disposition and storage object keys
# ---------------------------------------------------------------------------


def test_svg_and_html_are_never_inline():
    assert Image.get_content_disposition("image/svg+xml") is None
    assert Image.get_content_disposition("text/html") is None
    assert Image.get_content_disposition("image/png") == "inline"
    assert Image.get_content_disposition() == "inline"
    assert FileElement.get_content_disposition("image/png") is None


def test_storage_filename_is_one_component():
    assert Element.storage_filename("../../etc/passwd") == "passwd"
    assert Element.storage_filename("..\\..\\windows\\win.ini") == "win.ini"
    assert Element.storage_filename("a\x00b\n.txt") == "a_b_.txt"
    assert Element.storage_filename("") == "file"


# ---------------------------------------------------------------------------
# Upload streaming
# ---------------------------------------------------------------------------


def test_large_upload_streams_to_disk(client, tmp_path, monkeypatch):
    session = WebsocketSession(
        id="stream-session",
        socket_id="sid-stream",
        emit=AsyncMock(),
        emit_call=AsyncMock(),
        user_env={},
        client_type="webapp",
        user=None,
    )
    monkeypatch.setattr("chainlit.config.FILES_DIRECTORY", tmp_path)
    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(lambda _: session))
    monkeypatch.setattr("chainlit.server._UPLOAD_STREAM_THRESHOLD", 0)

    response = client.post(
        "/project/file",
        files={"file": ("big.txt", b"x" * 1024, "text/plain")},
        params={"session_id": session.id},
    )

    assert response.status_code == 200, response.text
    file_id = response.json()["id"]
    stored = session.files[file_id]["path"]
    assert stored.read_bytes() == b"x" * 1024


async def test_persist_file_enforces_actual_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr("chainlit.config.FILES_DIRECTORY", tmp_path)
    session = WebsocketSession(
        id="cap-session",
        socket_id="sid-cap",
        emit=AsyncMock(),
        emit_call=AsyncMock(),
        user_env={},
        client_type="webapp",
        user=None,
    )

    with pytest.raises(ValueError, match="File size too large"):
        await session.persist_file(
            name="x.txt",
            mime="text/plain",
            fileobj=io.BytesIO(b"0123456789"),
            max_bytes=5,
        )

    assert not list((tmp_path / "cap-session").glob("*"))


# ---------------------------------------------------------------------------
# Socket handshake input shaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    ['["x"]', '{"A": 1}', '{"A": null}', '"str"', "not-json"],
)
def test_load_user_env_rejects_non_flat_string_map(payload):
    from chainlit.socket import load_user_env

    with pytest.raises(ConnectionRefusedError):
        load_user_env(payload)


def test_load_user_env_accepts_flat_string_map():
    from chainlit.socket import load_user_env

    assert load_user_env('{"A": "x"}') == {"A": "x"}
    assert load_user_env("null") == {}


def test_settings_exposes_explicit_allow_origins(client, monkeypatch):
    monkeypatch.setattr(config.project, "allow_origins", ["https://embed.example"])
    response = client.get("/project/settings")

    assert response.status_code == 200
    assert response.json()["allowOrigins"] == ["https://embed.example"]


# ---------------------------------------------------------------------------
# Auth cookie Secure flag follows the request scheme
# ---------------------------------------------------------------------------


def _request(scheme: str):
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 443 if scheme == "https" else 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )


def test_auth_cookie_is_secure_on_https(monkeypatch):
    from starlette.responses import Response

    from chainlit.auth import cookie as cookie_module

    monkeypatch.setattr(cookie_module, "_cookie_secure", False)
    monkeypatch.delenv("CHAINLIT_URL", raising=False)
    response = Response()

    cookie_module.set_auth_cookie(_request("https"), response, "token")

    assert "secure" in response.headers["set-cookie"].lower()


def test_auth_cookie_stays_plain_on_http(monkeypatch):
    from starlette.responses import Response

    from chainlit.auth import cookie as cookie_module

    monkeypatch.setattr(cookie_module, "_cookie_secure", False)
    monkeypatch.delenv("CHAINLIT_URL", raising=False)
    response = Response()

    cookie_module.set_auth_cookie(_request("http"), response, "token")

    assert "secure" not in response.headers["set-cookie"].lower()


# ---------------------------------------------------------------------------
# Client messages are server-authoritative user turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_message_cannot_forge_type_or_author(
    mock_chainlit_context,
):
    import uuid as _uuid

    from chainlit.emitter import ChainlitEmitter
    from chainlit.user import User as ChatUser

    payload = {
        "message": {
            "id": str(_uuid.uuid4()),
            "createdAt": "2026-01-01T00:00:00Z",
            "output": "hello",
            "type": "assistant_message",
            "name": "Admin",
            "metadata": {"approval": {"id": "forged"}},
        },
        "fileReferences": None,
    }

    async with mock_chainlit_context as context:
        context.session.user = ChatUser(identifier="alice")
        context.session.thread_id = "thread-1"
        context.session.has_first_interaction = True
        emitter = ChainlitEmitter(context.session)

        message = await emitter.process_message(payload)

    assert message.type == "user_message"
    assert message.author == "alice"
    assert message.metadata == {}


@pytest.mark.asyncio
async def test_client_message_rejects_non_v4_ids(mock_chainlit_context):
    from chainlit.emitter import ChainlitEmitter

    payload = {
        "message": {
            "id": "not-a-uuid",
            "createdAt": "2026-01-01T00:00:00Z",
            "output": "hello",
        },
        "fileReferences": None,
    }

    async with mock_chainlit_context as context:
        context.session.thread_id = "thread-1"
        context.session.has_first_interaction = True
        emitter = ChainlitEmitter(context.session)

        with pytest.raises(ValueError, match="Invalid message id"):
            await emitter.process_message(payload)


# ---------------------------------------------------------------------------
# Socket thread authorization without login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_refuses_owned_thread_when_login_is_disabled():
    from unittest.mock import AsyncMock

    from chainlit.socket import connect

    auth = {
        "sessionId": "valid-session-id",
        "clientType": "webapp",
        "threadId": "0190a4b0-2f3c-4d5e-8f90-123456789abc",
    }
    layer = AsyncMock()
    layer.get_thread.return_value = {
        "id": auth["threadId"],
        "userIdentifier": "alice",
        "steps": [],
        "elements": [],
    }

    with (
        patch("chainlit.socket.require_login", return_value=False),
        patch("chainlit.socket.get_data_layer", return_value=layer),
        patch("chainlit.socket.WebsocketSession") as mock_session,
    ):
        with pytest.raises(ConnectionRefusedError, match="authorization failed"):
            await connect("sid", {}, auth)

    mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_connect_allows_ownerless_thread_when_login_is_disabled():
    from unittest.mock import AsyncMock

    from chainlit.socket import connect

    auth = {
        "sessionId": "valid-session-id",
        "clientType": "webapp",
        "threadId": "0190a4b0-2f3c-4d5e-8f90-123456789abc",
    }
    layer = AsyncMock()
    layer.get_thread.return_value = {
        "id": auth["threadId"],
        "userIdentifier": None,
        "steps": [],
        "elements": [],
    }

    with (
        patch("chainlit.socket.require_login", return_value=False),
        patch("chainlit.socket.get_data_layer", return_value=layer),
        patch("chainlit.socket.WebsocketSession") as mock_session,
    ):
        mock_session.get_by_id.return_value = None
        assert await connect("sid", {}, auth) is True

    mock_session.assert_called_once()


def test_downloaded_file_sets_no_referrer(client, tmp_path, monkeypatch):
    session = WebsocketSession(
        id="referrer-session",
        socket_id="sid-referrer",
        emit=AsyncMock(),
        emit_call=AsyncMock(),
        user_env={},
        client_type="webapp",
        user=None,
    )
    stored = tmp_path / "stored.txt"
    stored.write_bytes(b"body")
    session.files["file-1"] = {
        "id": "file-1",
        "path": stored,
        "name": "x.txt",
        "type": "text/plain",
        "size": 4,
    }
    monkeypatch.setattr(WebsocketSession, "get_by_id", staticmethod(lambda _: session))

    response = client.get("/project/file/file-1", params={"session_id": session.id})

    assert response.status_code == 200
    assert response.headers["referrer-policy"] == "no-referrer"


# ---------------------------------------------------------------------------
# DynamoDB authorization reads must be strongly consistent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dynamo_authorization_reads_use_consistent_read():
    from unittest.mock import Mock

    from chainlit.data.dynamodb import DynamoDBDataLayer

    client = Mock()
    client.get_item.return_value = {
        "Item": {"userId": {"S": "alice"}, "userIdentifier": {"S": "alice"}}
    }
    client.query.return_value = {
        "Items": [
            {
                "PK": {"S": "THREAD#thread-1"},
                "SK": {"S": "THREAD"},
                "id": {"S": "thread-1"},
                "createdAt": {"S": "2026-01-01T00:00:00Z"},
                "metadata": {"M": {}},
            }
        ]
    }
    layer = DynamoDBDataLayer("table", client=client)

    await layer.get_thread_author("thread-1")
    await layer.get_thread_owner("thread-1")
    await layer.get_element("thread-1", "element-1")
    await layer.get_thread("thread-1")

    assert all(
        call.kwargs.get("ConsistentRead") is True
        for call in client.get_item.call_args_list
    )
    assert client.query.call_args.kwargs["ConsistentRead"] is True


# ---------------------------------------------------------------------------
# HTTP audit F1-F5 regressions
# ---------------------------------------------------------------------------


def test_uncapable_feedback_layer_still_gates_foreign_threads(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    feedback_layer.feedback_authorization_supported = False
    feedback_layer.get_thread_author.return_value = BOB.identifier
    as_user(ALICE)

    with patch(
        "chainlit.session.WebsocketSession.get_by_id",
        return_value=_ws_session(ALICE),
    ):
        response = client.put("/feedback", json=_feedback_payload())

    assert response.status_code == 404
    feedback_layer.upsert_feedback.assert_not_awaited()


def test_uncapable_feedback_layer_allows_owner_new_feedback(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    feedback_layer.feedback_authorization_supported = False
    feedback_layer.get_thread_author.return_value = ALICE.identifier
    feedback_layer.step_belongs_to_thread.return_value = True
    feedback_layer.upsert_feedback.return_value = "feedback-1"
    as_user(ALICE)

    with patch(
        "chainlit.session.WebsocketSession.get_by_id",
        return_value=_ws_session(ALICE),
    ):
        response = client.put("/feedback", json=_feedback_payload())

    assert response.status_code == 200, response.text
    feedback_layer.upsert_feedback.assert_awaited_once()


def test_uncapable_feedback_layer_fails_closed_for_existing_ids(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr(config.features, "thread_grants", False)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    feedback_layer.feedback_authorization_supported = False
    feedback_layer.get_thread_author.return_value = ALICE.identifier
    feedback_layer.step_belongs_to_thread.return_value = True
    feedback_layer.get_feedback_thread_id.side_effect = NotImplementedError
    as_user(ALICE)

    payload = _feedback_payload()
    payload["feedback"]["id"] = "feedback-1"
    with patch(
        "chainlit.session.WebsocketSession.get_by_id",
        return_value=_ws_session(ALICE),
    ):
        response = client.put("/feedback", json=payload)

    assert response.status_code == 404
    feedback_layer.upsert_feedback.assert_not_awaited()


def test_anonymous_shared_thread_is_reachable_and_share_safe(
    client, feedback_layer, monkeypatch
):
    monkeypatch.setattr("chainlit.auth.require_login", lambda: True)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    monkeypatch.setattr(config.features, "thread_grants", False)
    feedback_layer.get_thread.return_value = {
        "id": "thread-1",
        "createdAt": "2026-01-01T00:00:00Z",
        "name": "chat",
        "userId": "alice-id",
        "userIdentifier": "alice@example.com",
        "metadata": {"is_shared": True},
        "steps": [],
        "elements": [
            {
                "id": "element-1",
                "objectKey": "alice/thread-1/private",
                "PK": "THREAD#thread-1",
                "SK": "ELEMENT#element-1",
                "url": "https://files.example/element-1",
            }
        ],
    }

    response = client.get("/project/share/thread-1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["userId"] is None
    assert body["userIdentifier"] is None
    element = body["elements"][0]
    assert "objectKey" not in element
    assert "PK" not in element
    assert "SK" not in element
    assert element["url"] == "https://files.example/element-1"


def test_named_share_element_hides_storage_keys(client, feedback_layer, monkeypatch):
    from chainlit.data.acl import ThreadAccess

    as_user(BOB)
    feedback_layer.get_element.return_value = {
        "id": "element-1",
        "objectKey": "alice/thread-1/private",
        "PK": "THREAD#thread-1",
        "SK": "ELEMENT#element-1",
        "url": "https://files.example/element-1",
    }

    with patch(
        "chainlit.server.authorize_thread_read",
        AsyncMock(return_value=ThreadAccess.SHARED),
    ):
        response = client.get("/project/thread/thread-1/element/element-1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "objectKey" not in body
    assert "PK" not in body
    assert "SK" not in body
    assert body["url"] == "https://files.example/element-1"


def test_static_paths_reject_nul_bytes(client):
    for path in ("/public/%00", "/assets/%00", "/copilot/%00"):
        response = client.get(path)
        assert response.status_code == 400, path


@pytest.mark.asyncio
async def test_teams_activity_without_authorization_is_401():
    from starlette.requests import Request

    from chainlit.server import _process_teams_activity

    request = Request(
        {
            "type": "http",
            "scheme": "https",
            "method": "POST",
            "path": "/teams/events",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1),
            "root_path": "",
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await _process_teams_activity(request)

    assert exc_info.value.status_code == 401


# ---------------------------------------------------------------------------
# Auth audit AUTH-05, AUTH-07, AUTH-08, AUTH-09
# ---------------------------------------------------------------------------


def test_https_login_rejects_http_origin(monkeypatch):
    secure_client = TestClient(app, base_url="https://testserver")
    callback = AsyncMock(return_value=ALICE)
    monkeypatch.setattr(config.code, "password_auth_callback", callback)
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "x" * 32)

    response = secure_client.post(
        "/login",
        data={"username": "alice", "password": "pw"},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 403
    callback.assert_not_awaited()


def test_logout_revokes_a_replayed_cookie(client, monkeypatch):
    monkeypatch.setattr(
        config.code, "password_auth_callback", AsyncMock(return_value=ALICE)
    )
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "x" * 32)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)
    monkeypatch.setattr("chainlit.auth.get_data_layer", lambda: None)

    login = client.post(
        "/login",
        data={"username": "alice", "password": "pw"},
        headers={"Origin": "http://testserver"},
    )
    assert login.status_code == 200, login.text
    token = login.cookies["access_token"]
    cookie = {"Cookie": f"access_token={token}"}

    assert client.get("/user", headers=cookie).status_code == 200
    assert (
        client.post(
            "/logout",
            headers={**cookie, "Origin": "http://testserver"},
        ).status_code
        == 200
    )
    assert client.get("/user", headers=cookie).status_code == 401


def test_oauth_error_redirect_is_root_relative():
    from chainlit.server import _get_oauth_redirect_error

    response = _get_oauth_redirect_error(_request("http"), "oauthSignin")

    location = response.headers["location"]
    assert location.startswith("/login?")
    assert "error=oauthSignin" in location


def test_oauth_state_and_nonce_cookies_are_secure_on_https(monkeypatch):
    from starlette.responses import Response

    from chainlit.auth import cookie as cookie_module

    monkeypatch.setattr(cookie_module, "_cookie_secure", False)
    request = _request("https")
    response = Response()

    cookie_module.set_oauth_state_cookie(response, "state", request)
    cookie_module.set_oauth_nonce_cookie(response, "nonce", request)

    headers = response.headers.getlist("set-cookie")
    assert len(headers) == 2
    assert all("secure" in header.lower() for header in headers)


def test_password_login_is_rate_limited(client, monkeypatch):
    import chainlit.server as server_module

    callback = AsyncMock(return_value=None)
    monkeypatch.setattr(config.code, "password_auth_callback", callback)
    monkeypatch.setattr(server_module, "_LOGIN_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(server_module, "_LOGIN_WINDOW_SECONDS", 60)

    def attempt():
        return client.post(
            "/login",
            data={"username": "rate-limit-user", "password": "wrong"},
            headers={"Origin": "http://testserver"},
        )

    assert attempt().status_code == 401
    assert attempt().status_code == 401
    limited = attempt()

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert callback.await_count == 2


@pytest.mark.asyncio
async def test_approval_guard_fails_closed_on_state_store_outage(monkeypatch):
    from chainlit.approval import ApprovalPending, guard_pending_approval

    store = Mock()
    store.set_members = AsyncMock(side_effect=RuntimeError("state store down"))
    monkeypatch.setattr("chainlit.approval.get_state_store", lambda: store)

    with pytest.raises(ApprovalPending, match="Cannot verify"):
        await guard_pending_approval("thread-1")


def test_mcp_connection_cap_counts_pending_and_attached(monkeypatch):
    import chainlit.server as server_module

    monkeypatch.setattr(server_module, "_MAX_MCP_SESSIONS", 2)
    session = Mock()
    session.mcp_sessions = {"a": object(), "b": object()}
    session.mcp_oauth_tasks = {}

    assert server_module._mcp_capacity_reached(session, "new") is True
    assert server_module._mcp_capacity_reached(session, "a") is False

    session.mcp_sessions = {"a": object()}
    session.mcp_oauth_tasks = {"pending": object()}
    assert server_module._mcp_capacity_reached(session, "new") is True
    assert server_module._mcp_capacity_reached(session, "pending") is False


# ---------------------------------------------------------------------------
# Race audit regressions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_runs_are_serialized():
    from chainlit.socket import _start_run

    session = Mock()
    session.run_tasks = set()
    session.run_lock = asyncio.Lock()
    session.current_task = None
    release = asyncio.Event()
    order = []

    async def first():
        order.append("first-start")
        await release.wait()
        order.append("first-end")

    async def second():
        order.append("second")

    first_task = _start_run(session, first())
    second_task = _start_run(session, second())
    assert first_task is not None
    assert second_task is not None
    await asyncio.sleep(0)
    assert order == ["first-start"]

    release.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-start", "first-end", "second"]


def test_replayed_client_message_id_is_ignored():
    import uuid as _uuid

    from chainlit.socket import _mark_message_once

    session = Mock()
    session.processed_message_ids = set()
    message_id = str(_uuid.uuid4())
    payload = {"message": {"id": message_id, "output": "hi"}}

    assert _mark_message_once(session, payload) is True
    assert _mark_message_once(session, payload) is False
    assert (
        _mark_message_once(
            session, {"message": {"id": str(_uuid.uuid4()), "output": "hi"}}
        )
        is True
    )
    assert _mark_message_once(session, {"message": {"id": "not-a-uuid"}}) is True


@pytest.mark.asyncio
async def test_audio_callback_backlog_is_bounded(monkeypatch):
    import chainlit.socket as socket_module
    from chainlit.socket import _start_audio_task

    monkeypatch.setattr(socket_module, "_MAX_AUDIO_TASKS", 1)
    session = Mock()
    session.audio_tasks = set()
    session.audio_lock = asyncio.Lock()
    release = asyncio.Event()

    async def chunk():
        await release.wait()

    first = _start_audio_task(session, chunk())
    second = _start_audio_task(session, chunk())

    assert first is not None
    assert second is None
    release.set()
    await first


@pytest.mark.asyncio
async def test_teams_activity_rejects_an_unexpected_tenant(monkeypatch):
    from chainlit.server import _process_teams_activity

    monkeypatch.setenv("TEAMS_APP_TENANT_ID", "allowed-tenant")
    request = Mock()
    request.headers = {"Authorization": "Bearer token"}
    request.json = AsyncMock(
        return_value={
            "type": "message",
            "conversation": {"id": "c1", "tenantId": "other-tenant"},
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await _process_teams_activity(request, adapter=Mock(), bot=Mock())

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_oauth_identity_collision_is_namespaced():
    from chainlit.auth import resolve_provider_identity
    from chainlit.user import PersistedUser

    existing = PersistedUser(
        id="google-id",
        createdAt="2026-01-01T00:00:00Z",
        identifier="victim@example.com",
        metadata={"provider": "google"},
    )
    layer = Mock()
    layer.get_user = AsyncMock(return_value=existing)
    incoming = User(
        identifier="victim@example.com",
        metadata={"provider": "github"},
    )

    resolved = await resolve_provider_identity(incoming, layer)

    assert resolved.identifier == "github:victim@example.com"
    assert resolved.metadata == {"provider": "github"}
