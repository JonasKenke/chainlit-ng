"""HTTP tenancy boundaries: thread/element ACLs and shared-thread exposure.

The data layer is mocked so each test can assert two things at once: the
caller gets a denial, and the data-layer mutator is never reached.
"""

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

        # The route currently answers 500 for an unknown thread. The security
        # property under test is that no thread body is returned.
        assert response.status_code == 500
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "F2 in docs/research/security-threat-model.md: /project/element authorizes "
        "the caller's session but not the target element's owner"
    ),
)
def test_cross_tenant_custom_element_delete_is_rejected(client, data_layer):
    as_user(BOB)
    bob_session = Mock(spec=WebsocketSession)
    bob_session.id = "bob-session"
    bob_session.user = BOB
    bob_session.thread_id = "bob-thread"
    bob_session.has_first_interaction = True
    bob_session.files = {}
    bob_session.files_spec = {}
    bob_session.emit = AsyncMock()

    with (
        patch("chainlit.session.WebsocketSession.get_by_id", return_value=bob_session),
        patch("chainlit.element.get_data_layer", return_value=data_layer),
    ):
        response = client.request(
            "DELETE",
            "/project/element",
            json={
                "sessionId": "bob-session",
                "element": {
                    "id": "alice-element-id",
                    "type": "custom",
                    "name": "alice-element",
                    "display": "side",
                    "threadId": "alice-thread",
                },
            },
        )

    assert response.status_code == 200
    data_layer.delete_element.assert_not_awaited()
