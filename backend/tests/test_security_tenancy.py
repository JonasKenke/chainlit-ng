"""HTTP tenancy boundaries: thread/element ACLs and shared-thread exposure.

The data layer is mocked so each test can assert two things at once: the
caller gets a denial, and the data-layer mutator is never reached.
"""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from chainlit.auth import get_current_user
from chainlit.context import context_var
from chainlit.data.base import BaseDataLayer
from chainlit.server import app
from chainlit.session import WebsocketSession
from chainlit.thread_metadata import public_thread
from chainlit.types import PageInfo, PaginatedResponse, Pagination
from chainlit.user import PersistedUser

ALICE = PersistedUser(
    id="alice-id", identifier="alice", createdAt="2026-01-01T00:00:00Z"
)
BOB = PersistedUser(id="bob-id", identifier="bob", createdAt="2026-01-01T00:00:00Z")


@pytest.fixture(autouse=True)
def isolate_context():
    token = context_var.set(object())
    try:
        yield
    finally:
        context_var.reset(token)


@pytest.fixture
def client():
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def data_layer():
    layer = AsyncMock(spec=BaseDataLayer)
    return layer


def as_user(user):
    app.dependency_overrides[get_current_user] = lambda: user


def thread(metadata=None):
    return {
        "id": "thread-1",
        "createdAt": "2026-01-01T00:00:00Z",
        "name": "chat",
        "userId": ALICE.id,
        "userIdentifier": ALICE.identifier,
        "metadata": metadata or {},
        "steps": [],
        "elements": [],
    }


class TestThreadAcl:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-1", None),
            ("PUT", "/project/thread", {"threadId": "thread-1", "name": "pwn"}),
            (
                "PUT",
                "/project/thread/share",
                {"threadId": "thread-1", "isShared": True},
            ),
            ("DELETE", "/project/thread", {"threadId": "thread-1"}),
            (
                "GET",
                "/project/thread/thread-1/element/element-1",
                None,
            ),
        ],
    )
    def test_non_author_is_denied_before_the_data_layer(
        self, client, data_layer, method, path, payload
    ):
        as_user(BOB)
        data_layer.get_thread_author.return_value = ALICE.identifier

        with (
            patch("chainlit.server.get_data_layer", return_value=data_layer),
            patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
        ):
            response = client.request(method, path, json=payload)

        assert response.status_code == 401
        for mutator in (
            data_layer.get_thread,
            data_layer.update_thread,
            data_layer.delete_thread,
            data_layer.get_element,
        ):
            mutator.assert_not_awaited()

    def test_missing_thread_does_not_reach_the_data_layer(self, data_layer):
        raw_client = TestClient(app, raise_server_exceptions=False)
        as_user(BOB)
        data_layer.get_thread_author.side_effect = ValueError("Author not found")

        with (
            patch("chainlit.server.get_data_layer", return_value=data_layer),
            patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
        ):
            response = raw_client.get("/project/thread/thread-404")

        # An unknown thread is a 404 and must not return a thread body.
        assert response.status_code == 404
        data_layer.get_thread.assert_not_awaited()


class TestThreadListFilter:
    def test_client_supplied_user_id_is_overwritten(self, client, data_layer):
        as_user(BOB)
        data_layer.list_threads.return_value = PaginatedResponse(
            pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
            data=[],
        )

        with patch("chainlit.server.get_data_layer", return_value=data_layer):
            response = client.post(
                "/project/threads",
                json={
                    "pagination": Pagination(first=10).model_dump(),
                    "filter": {"userId": ALICE.id},
                },
            )

        assert response.status_code == 200
        filters = data_layer.list_threads.await_args.args[1]
        assert filters.userId == BOB.id


class TestSharedThreadExposure:
    def test_unshared_thread_is_404(self, client, data_layer):
        data_layer.get_thread.return_value = thread(metadata={"is_shared": False})

        with patch("chainlit.server.get_data_layer", return_value=data_layer):
            response = client.get("/project/share/thread-1")

        assert response.status_code == 404

    def test_shared_thread_hides_internal_metadata(self, client, data_layer):
        stored = thread(
            metadata={
                "is_shared": True,
                "chat_settings": {"model": "private-model"},
                "chat_profile": "private-profile",
                "env": {"OPENAI_API_KEY": "sk-not-for-the-client"},
                "custom": "hidden",
            }
        )
        data_layer.get_thread.return_value = stored

        with patch("chainlit.server.get_data_layer", return_value=data_layer):
            response = client.get("/project/share/thread-1")

        assert response.status_code == 200
        body = response.json()
        assert body["metadata"] == {"is_shared": True}
        assert "sk-not-for-the-client" not in response.text
        assert public_thread(stored)["metadata"] == {"is_shared": True}

    def test_share_view_hides_approval_detail(self, client, data_layer):
        approval = {
            "id": "approval-1",
            "stepId": "step-1",
            "threadId": "thread-1",
            "tool": "send_email",
            "title": "Send the draft",
            "status": "approved",
            "requestedBy": "alice@example.com",
            "requestedAt": "2026-01-01T00:00:00Z",
            "decidedBy": "alice@example.com",
            "decidedAt": "2026-01-01T00:00:05Z",
            "decision": "approve",
            "terminalReason": None,
            "argumentsPreview": {"to": "person@example.com", "apiKey": "[redacted]"},
            "argumentsDigest": "hmac-sha256:secret-digest",
            "digestSalt": "secret-salt",
            "decisionRequestId": "request-1",
        }
        stored = thread(metadata={"is_shared": True})
        stored["steps"] = [
            {
                "id": "step-1",
                "threadId": "thread-1",
                "input": json.dumps(
                    {
                        "approval": {
                            "argumentsPreview": {
                                "to": "person@example.com",
                                "edited": "edited@example.com",
                            }
                        }
                    }
                ),
                "metadata": {"keep": "step-meta", "approval": approval},
            }
        ]
        data_layer.get_thread.return_value = stored

        with patch("chainlit.server.get_data_layer", return_value=data_layer):
            response = client.get("/project/share/thread-1")

        assert response.status_code == 200
        public_metadata = response.json()["steps"][0]["metadata"]
        assert public_metadata == {
            "keep": "step-meta",
            "approval": {
                "status": "approved",
                "tool": "send_email",
                "decision": "approve",
                "decidedAt": "2026-01-01T00:00:05Z",
                "terminalReason": None,
            },
        }
        assert "input" not in response.json()["steps"][0]
        for hidden in (
            "person@example.com",
            "edited@example.com",
            "secret-digest",
            "secret-salt",
            "request-1",
            "alice@example.com",
        ):
            assert hidden not in response.text

        owner_approval = public_thread(stored, include_settings=True)["steps"][0][
            "metadata"
        ]["approval"]
        assert owner_approval == approval

    @pytest.mark.parametrize(
        "metadata",
        [
            "not-json",
            '{"approval": "unexpected", "other": "kept"}',
            '{"approval": null, "argumentsPreview": {"secret": "x"}}',
            '{"approval": ["not", "a", "dict"]}',
            ["not", "a", "dict"],
        ],
    )
    def test_share_view_fails_closed_on_unexpected_metadata(
        self, client, data_layer, metadata
    ):
        stored = thread(metadata={"is_shared": True})
        stored["steps"] = [
            {"id": "step-1", "threadId": "thread-1", "metadata": metadata}
        ]
        data_layer.get_thread.return_value = stored

        with patch("chainlit.server.get_data_layer", return_value=data_layer):
            response = client.get("/project/share/thread-1")

        assert response.status_code == 200
        public_metadata = response.json()["steps"][0]["metadata"]
        assert "approval" not in public_metadata
        assert "argumentsPreview" not in public_metadata
        assert "unexpected" not in response.text

    def test_callback_denial_wins_over_missing_share_flag(self, client, data_layer):
        async def deny(_thread, _user):
            return False

        data_layer.get_thread.return_value = thread(metadata={})
        with (
            patch("chainlit.server.get_data_layer", return_value=data_layer),
            patch("chainlit.server.config.code.on_shared_thread_view", deny),
        ):
            response = client.get("/project/share/thread-1")

        assert response.status_code == 404


def _websocket_session(user, thread_id):
    session = Mock(spec=WebsocketSession)
    session.id = f"{user.identifier}-session"
    session.user = user
    session.thread_id = thread_id
    session.has_first_interaction = True
    session.files = {}
    session.files_spec = {}
    session.emit = AsyncMock()
    session.persist_file = AsyncMock(return_value={"id": "chainlit-key"})
    return session


def _element_payload(element_id, thread_id):
    return {
        "sessionId": "session-id",
        "element": {
            "id": element_id,
            "type": "custom",
            "name": "custom-element",
            "display": "side",
            "forId": "message-id",
            "threadId": thread_id,
        },
    }


def _assert_cross_tenant_mutation_is_rejected(client, data_layer, method, mutator):
    as_user(BOB)
    bob_session = _websocket_session(BOB, "bob-thread")
    data_layer.get_element.return_value = None
    data_layer.get_thread_author.return_value = ALICE.identifier

    with (
        patch("chainlit.session.WebsocketSession.get_by_id", return_value=bob_session),
        patch("chainlit.server.get_data_layer", return_value=data_layer),
        patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
        patch("chainlit.element.get_data_layer", return_value=data_layer),
    ):
        response = client.request(
            method,
            "/project/element",
            # The body claims Alice's thread and element; the route must use
            # Bob's server-side session thread and reject the mutation.
            json=_element_payload("alice-element-id", "alice-thread"),
        )

    assert response.status_code == 404
    getattr(data_layer, mutator).assert_not_awaited()


def test_cross_tenant_custom_element_delete_is_rejected(client, data_layer):
    _assert_cross_tenant_mutation_is_rejected(
        client, data_layer, "DELETE", "delete_element"
    )


def test_cross_tenant_custom_element_update_is_rejected(client, data_layer):
    _assert_cross_tenant_mutation_is_rejected(
        client, data_layer, "PUT", "create_element"
    )


def test_non_author_of_the_session_thread_is_rejected(client, data_layer):
    as_user(BOB)
    bob_session = _websocket_session(BOB, "alice-thread")
    data_layer.get_element.return_value = {"id": "alice-element-id"}
    data_layer.get_thread_author.return_value = ALICE.identifier

    with (
        patch("chainlit.session.WebsocketSession.get_by_id", return_value=bob_session),
        patch("chainlit.server.get_data_layer", return_value=data_layer),
        patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
        patch("chainlit.element.get_data_layer", return_value=data_layer),
    ):
        response = client.request(
            "DELETE",
            "/project/element",
            json=_element_payload("alice-element-id", "alice-thread"),
        )

    assert response.status_code == 401
    data_layer.delete_element.assert_not_awaited()


def test_owner_can_delete_element_in_the_session_thread(client, data_layer):
    as_user(ALICE)
    alice_session = _websocket_session(ALICE, "alice-thread")
    data_layer.get_element.return_value = {"id": "alice-element-id"}
    data_layer.get_thread_author.return_value = ALICE.identifier

    with (
        patch(
            "chainlit.session.WebsocketSession.get_by_id", return_value=alice_session
        ),
        patch("chainlit.server.get_data_layer", return_value=data_layer),
        patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
        patch("chainlit.element.get_data_layer", return_value=data_layer),
    ):
        response = client.request(
            "DELETE",
            "/project/element",
            # A forged thread id must not override the session thread.
            json=_element_payload("alice-element-id", "other-thread"),
        )

    assert response.status_code == 200
    data_layer.delete_element.assert_awaited_once_with(
        "alice-element-id", "alice-thread"
    )


def test_owner_can_update_element_in_the_session_thread(client, data_layer):
    as_user(ALICE)
    alice_session = _websocket_session(ALICE, "alice-thread")
    data_layer.get_element.return_value = {"id": "alice-element-id"}
    data_layer.get_thread_author.return_value = ALICE.identifier

    with (
        patch(
            "chainlit.session.WebsocketSession.get_by_id", return_value=alice_session
        ),
        patch("chainlit.server.get_data_layer", return_value=data_layer),
        patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
        patch("chainlit.element.get_data_layer", return_value=data_layer),
    ):
        response = client.request(
            "PUT",
            "/project/element",
            json=_element_payload("alice-element-id", "other-thread"),
        )

    assert response.status_code == 200
    data_layer.get_element.assert_awaited_once_with("alice-thread", "alice-element-id")
