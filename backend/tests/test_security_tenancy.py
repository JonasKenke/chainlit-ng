"""HTTP tenancy boundaries: thread/element ACLs and shared-thread exposure.

The data layer is mocked so each test can assert two things at once: the
caller gets a denial, and the data-layer mutator is never reached.
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.context import context_var
from chainlit.data.acl import (
    MAX_THREAD_GRANTS_PER_THREAD,
    ThreadAccess,
    authorize_thread_read,
)
from chainlit.data.base import BaseDataLayer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.server import app
from chainlit.session import WebsocketSession
from chainlit.thread_metadata import public_thread
from chainlit.types import (
    FeedbackThreadLookup,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadGrant,
    ThreadOwner,
)
from chainlit.user import PersistedUser, User

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


@pytest.fixture
def grants_on(monkeypatch):
    monkeypatch.setattr(config.features, "thread_grants", True)


@pytest.fixture
def grants_active(monkeypatch, data_layer):
    """The full opt-in: flag, login, and a capable layer."""
    monkeypatch.setattr(config.features, "thread_grants", True)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    data_layer.thread_grants_supported = True
    return data_layer


@pytest.fixture
def patch_layer(data_layer):
    with (
        patch("chainlit.server.get_data_layer", return_value=data_layer),
        patch("chainlit.data.acl.get_data_layer", return_value=data_layer),
    ):
        yield data_layer


def grant(user=BOB):
    return ThreadGrant(
        thread_id="thread-1",
        grantee_user_id=user.id,
        grantee_user_created_at=user.createdAt,
        grantee_identifier=user.identifier,
        granted_by_user_id=ALICE.id,
        granted_at="2026-01-01T00:00:00Z",
    )


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


def ownerless_thread(metadata=None):
    row = thread(metadata)
    row["userId"] = None
    row["userIdentifier"] = None
    return row


class TestAuthorizeThreadRead:
    """The six outcomes of the single read chokepoint."""

    @pytest.mark.asyncio
    async def test_missing_thread_is_missing(self, data_layer):
        data_layer.get_thread_author.side_effect = ValueError("not found")
        access = await authorize_thread_read("t1", ALICE, data_layer=data_layer)
        assert access == ThreadAccess.MISSING

    @pytest.mark.asyncio
    async def test_foreign_thread_is_none(self, data_layer):
        data_layer.get_thread_author.return_value = ALICE.identifier
        access = await authorize_thread_read("t1", BOB, data_layer=data_layer)
        assert access == ThreadAccess.NONE

    @pytest.mark.asyncio
    async def test_owner_by_identifier(self, data_layer):
        data_layer.get_thread_author.return_value = ALICE.identifier
        access = await authorize_thread_read("t1", ALICE, data_layer=data_layer)
        assert access == ThreadAccess.OWNER

    @pytest.mark.asyncio
    async def test_ownerless_row_is_ownerless(self, data_layer):
        access = await authorize_thread_read(
            "t1", ALICE, thread=ownerless_thread(), data_layer=data_layer
        )
        assert access == ThreadAccess.OWNERLESS
        data_layer.get_thread_author.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_public_share(self, data_layer):
        access = await authorize_thread_read(
            "t1",
            None,
            thread=thread(metadata={"is_shared": True}),
            allow_public_share=True,
            data_layer=data_layer,
        )
        assert access == ThreadAccess.PUBLIC

    @pytest.mark.asyncio
    async def test_public_share_callback(self, data_layer):
        async def allow(_thread, _user):
            return True

        with patch("chainlit.data.acl.config.code.on_shared_thread_view", allow):
            access = await authorize_thread_read(
                "t1",
                ALICE,
                thread=thread(),
                allow_public_share=True,
                data_layer=data_layer,
            )

        assert access == ThreadAccess.PUBLIC

    @pytest.mark.asyncio
    async def test_public_branch_does_not_apply_to_owner_routes(self, data_layer):
        access = await authorize_thread_read(
            "t1",
            BOB,
            thread=thread(metadata={"is_shared": True}),
            data_layer=data_layer,
        )
        assert access == ThreadAccess.NONE

    @pytest.mark.asyncio
    async def test_owner_on_unshared_thread_is_not_owner_on_the_share_route(
        self, data_layer
    ):
        access = await authorize_thread_read(
            "t1",
            ALICE,
            thread=thread(metadata={"is_shared": False}),
            allow_public_share=True,
            data_layer=data_layer,
        )
        assert access == ThreadAccess.NONE

    @pytest.mark.asyncio
    async def test_owner_on_unshared_thread_is_owner_when_the_flag_is_on(
        self, data_layer, grants_on
    ):
        access = await authorize_thread_read(
            "t1",
            ALICE,
            thread=thread(metadata={"is_shared": False}),
            allow_public_share=True,
            data_layer=data_layer,
        )
        assert access == ThreadAccess.OWNER

    @pytest.mark.asyncio
    async def test_flag_on_uses_the_persisted_id(self, data_layer, grants_on):
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        access = await authorize_thread_read("t1", ALICE, data_layer=data_layer)
        assert access == ThreadAccess.OWNER

    @pytest.mark.asyncio
    async def test_flag_on_does_not_rescue_a_mismatched_row_by_identifier(
        self, data_layer, grants_on
    ):
        # The row belongs to bob's id but carries alice's identifier.
        row = thread()
        row["userId"] = BOB.id
        data_layer.get_thread_owner.return_value = ThreadOwner(
            user_id=BOB.id, identifier=ALICE.identifier
        )
        access = await authorize_thread_read("t1", ALICE, data_layer=data_layer)
        assert access == ThreadAccess.NONE

    @pytest.mark.asyncio
    async def test_flag_on_ownerless_row(self, data_layer, grants_on):
        data_layer.get_thread_owner.return_value = ThreadOwner()
        access = await authorize_thread_read("t1", ALICE, data_layer=data_layer)
        assert access == ThreadAccess.OWNERLESS

    @pytest.mark.asyncio
    async def test_flag_off_never_reads_grants(self, data_layer):
        data_layer.get_thread_author.return_value = ALICE.identifier
        await authorize_thread_read("t1", BOB, data_layer=data_layer)
        data_layer.get_thread_grant.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shared_with_matching_generation(self, data_layer, grants_on):
        data_layer.thread_grants_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_thread_grant.return_value = grant()
        access = await authorize_thread_read("t1", BOB, data_layer=data_layer)
        assert access == ThreadAccess.SHARED
        data_layer.get_thread_grant.assert_awaited_once_with("t1", BOB.id)

    @pytest.mark.asyncio
    async def test_shared_denied_when_the_layer_reports_no_capability(
        self, data_layer, grants_on
    ):
        data_layer.thread_grants_supported = False
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_thread_grant.return_value = grant()
        access = await authorize_thread_read("t1", BOB, data_layer=data_layer)
        assert access == ThreadAccess.NONE
        data_layer.get_thread_grant.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mutation",
        [
            {"grantee_user_id": "somebody-else"},
            {"grantee_user_created_at": "2020-01-01T00:00:00Z"},
            {"grantee_user_created_at": None},
            {"role": "write"},
        ],
    )
    async def test_shared_denied_for_a_non_matching_grant(
        self, data_layer, grants_on, mutation
    ):
        data_layer.thread_grants_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        stored = grant()
        for key, value in mutation.items():
            setattr(stored, key, value)
        data_layer.get_thread_grant.return_value = stored
        access = await authorize_thread_read("t1", BOB, data_layer=data_layer)
        assert access == ThreadAccess.NONE

    @pytest.mark.asyncio
    async def test_grant_lookup_error_denies(self, data_layer, grants_on):
        data_layer.thread_grants_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_thread_grant.side_effect = RuntimeError("db down")
        access = await authorize_thread_read("t1", BOB, data_layer=data_layer)
        assert access == ThreadAccess.NONE

    @pytest.mark.asyncio
    async def test_grant_lookup_error_still_allows_a_public_share(
        self, data_layer, grants_on
    ):
        data_layer.thread_grants_supported = True
        data_layer.get_thread_grant.side_effect = RuntimeError("db down")
        access = await authorize_thread_read(
            "t1",
            BOB,
            thread=thread(metadata={"is_shared": True}),
            allow_public_share=True,
            data_layer=data_layer,
        )
        assert access == ThreadAccess.PUBLIC

    @pytest.mark.asyncio
    async def test_owner_lookup_error_propagates_on_owner_routes(
        self, data_layer, grants_on
    ):
        data_layer.get_thread_owner.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError):
            await authorize_thread_read("t1", ALICE, data_layer=data_layer)

    @pytest.mark.asyncio
    async def test_default_owner_lookup_uses_the_legacy_author(self, data_layer):
        data_layer.get_thread_author.return_value = ALICE.identifier
        owner = await BaseDataLayer.get_thread_owner(data_layer, "t1")
        assert owner == ThreadOwner(identifier=ALICE.identifier)

    @pytest.mark.asyncio
    async def test_default_owner_lookup_returns_none_for_a_missing_row(
        self, data_layer
    ):
        data_layer.get_thread_author.side_effect = ValueError("not found")
        assert await BaseDataLayer.get_thread_owner(data_layer, "t1") is None


class TestThreadAccessRoutes:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-1", None),
            ("GET", "/project/thread/thread-1/element/element-1", None),
        ],
    )
    def test_ownerless_thread_is_404(
        self, client, data_layer, grants_on, patch_layer, method, path, payload
    ):
        as_user(ALICE)
        data_layer.get_thread_owner.return_value = ThreadOwner()

        response = client.request(method, path, json=payload)

        assert response.status_code == 404
        data_layer.get_element.assert_not_awaited()
        data_layer.get_thread.assert_not_awaited()

    def test_ownerless_thread_is_not_public_without_the_share_flag(
        self, client, data_layer, grants_on, patch_layer
    ):
        as_user(ALICE)
        data_layer.get_thread.return_value = ownerless_thread()

        response = client.get("/project/share/thread-1")

        assert response.status_code == 404

    def test_ownerless_shared_thread_is_still_public(
        self, client, data_layer, grants_on, patch_layer
    ):
        as_user(ALICE)
        data_layer.get_thread.return_value = ownerless_thread({"is_shared": True})

        response = client.get("/project/share/thread-1")

        assert response.status_code == 200

    def test_grantee_can_read_an_element_but_not_the_thread(
        self, client, data_layer, grants_on, patch_layer
    ):
        as_user(BOB)
        data_layer.thread_grants_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_thread_grant.return_value = grant()
        data_layer.get_element.return_value = {"id": "element-1"}
        data_layer.get_thread.return_value = thread(metadata={"is_shared": True})

        element = client.get("/project/thread/thread-1/element/element-1")
        owner_view = client.get("/project/thread/thread-1")
        share = client.get("/project/share/thread-1")

        assert element.status_code == 200
        assert owner_view.status_code == 401
        assert share.status_code == 200
        # The owner-only thread view never reads on a denial.
        data_layer.get_thread.assert_awaited_once()

    def test_shared_thread_response_does_not_require_owner_identity(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(BOB)
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_thread_grant.return_value = grant()
        data_layer.get_thread.return_value = thread(metadata={"is_shared": False})

        response = client.get("/project/share/thread-1")

        assert response.status_code == 200
        assert response.json()["userId"] is None
        assert response.json()["userIdentifier"] is None

    def test_share_response_keeps_the_owner_identity_while_the_flag_is_off(
        self, client, data_layer, patch_layer
    ):
        as_user(ALICE)
        stored = thread(metadata={"is_shared": True})
        stored["PK"] = "THREAD#thread-1"
        stored["SK"] = "THREAD"
        stored["UserThreadPK"] = f"USER#{ALICE.identifier}"
        stored["UserThreadSK"] = f"THREAD#{stored['id']}"
        data_layer.get_thread.return_value = stored

        response = client.get("/project/share/thread-1")

        assert response.status_code == 200
        body = response.json()
        assert body["userId"] == ALICE.id
        assert body["userIdentifier"] == ALICE.identifier
        assert body["UserThreadPK"] == f"USER#{ALICE.identifier}"

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-1", None),
            ("GET", "/project/thread/thread-1/element/element-1", None),
            ("GET", "/project/share/thread-1", None),
            ("GET", "/project/thread/thread-1/grants", None),
        ],
    )
    def test_thread_reads_are_not_cacheable(
        self, client, data_layer, grants_active, patch_layer, method, path, payload
    ):
        as_user(ALICE)
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_thread.return_value = thread(metadata={"is_shared": True})
        data_layer.get_element.return_value = {"id": "element-1"}
        data_layer.list_thread_grants.return_value = [grant()]

        response = client.request(method, path, json=payload)

        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "private, no-store"
        if path.endswith("/grants"):
            assert response.json()[0]["grantee_user_id"] == BOB.id

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-1/grants", None),
            ("POST", "/project/thread/thread-1/grants", {"grantee": "bob"}),
            ("DELETE", "/project/thread/thread-1/grants/bob-id", None),
        ],
    )
    def test_grant_routes_are_hidden_while_the_flag_is_off(
        self, client, data_layer, patch_layer, method, path, payload
    ):
        as_user(ALICE)

        response = client.request(method, path, json=payload)

        assert response.status_code == 404
        data_layer.list_thread_grants.assert_not_awaited()
        data_layer.create_thread_grant.assert_not_awaited()
        data_layer.delete_thread_grant.assert_not_awaited()

    def test_grant_create_is_404_without_a_data_layer(self, client):
        as_user(ALICE)
        with patch("chainlit.server.get_data_layer", return_value=None):
            response = client.post(
                "/project/thread/thread-1/grants", json={"grantee": "bob"}
            )

        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-1/grants", None),
            ("POST", "/project/thread/thread-1/grants", {"grantee": "bob"}),
            ("DELETE", "/project/thread/thread-1/grants/bob-id", None),
        ],
    )
    def test_grant_routes_are_501_without_layer_support(
        self, client, data_layer, grants_on, patch_layer, method, path, payload
    ):
        as_user(ALICE)
        data_layer.thread_grants_supported = False

        response = client.request(method, path, json=payload)

        assert response.status_code == 501
        data_layer.create_thread_grant.assert_not_awaited()
        data_layer.delete_thread_grant.assert_not_awaited()

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-1/grants", None),
            ("POST", "/project/thread/thread-1/grants", {"grantee": "bob"}),
            ("DELETE", "/project/thread/thread-1/grants/bob-id", None),
        ],
    )
    def test_grant_routes_are_owner_only(
        self, client, data_layer, grants_active, patch_layer, method, path, payload
    ):
        as_user(BOB)
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)

        response = client.request(method, path, json=payload)

        assert response.status_code == 404
        data_layer.list_thread_grants.assert_not_awaited()
        data_layer.create_thread_grant.assert_not_awaited()
        data_layer.delete_thread_grant.assert_not_awaited()

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("GET", "/project/thread/thread-404/grants", None),
            ("POST", "/project/thread/thread-404/grants", {"grantee": "bob"}),
            ("DELETE", "/project/thread/thread-404/grants/bob-id", None),
        ],
    )
    def test_grant_routes_hide_a_missing_thread(
        self, client, data_layer, grants_active, patch_layer, method, path, payload
    ):
        as_user(ALICE)
        data_layer.get_thread_owner.return_value = None

        response = client.request(method, path, json=payload)

        assert response.status_code == 404
        data_layer.create_thread_grant.assert_not_awaited()
        data_layer.delete_thread_grant.assert_not_awaited()

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            ("POST", "/project/thread/thread-1/grants", {"grantee": "bob"}),
            ("DELETE", "/project/thread/thread-1/grants/bob-id", None),
        ],
    )
    def test_cross_origin_grant_mutations_are_rejected(
        self, client, data_layer, grants_active, patch_layer, method, path, payload
    ):
        as_user(ALICE)

        response = client.request(
            method, path, json=payload, headers={"Origin": "https://evil.example"}
        )

        assert response.status_code == 403
        data_layer.get_thread_owner.assert_not_awaited()
        data_layer.create_thread_grant.assert_not_awaited()
        data_layer.delete_thread_grant.assert_not_awaited()

    def test_same_origin_grant_mutation_is_allowed(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.list_thread_grants.return_value = []

        response = client.delete(
            "/project/thread/thread-1/grants/bob-id",
            headers={"Origin": "http://testserver"},
        )

        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        data_layer.delete_thread_grant.assert_awaited_once_with("thread-1", "bob-id")

    def test_flag_on_write_auth_uses_the_quiet_user_lookup(
        self, client, data_layer, grants_on, patch_layer
    ):
        as_user(ALICE)
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.get_user_for_acl.return_value = ALICE

        response = client.put(
            "/project/thread", json={"threadId": "thread-1", "name": "renamed"}
        )

        assert response.status_code == 200, response.text
        data_layer.get_user_for_acl.assert_awaited_once_with(ALICE.identifier)
        data_layer.get_user.assert_not_awaited()

    def test_flag_on_write_auth_denies_a_foreign_typed_owner(
        self, client, data_layer, grants_on, patch_layer
    ):
        as_user(ALICE)
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=BOB.id)
        data_layer.get_user_for_acl.return_value = ALICE

        response = client.put(
            "/project/thread", json={"threadId": "thread-1", "name": "renamed"}
        )

        assert response.status_code == 401
        data_layer.update_thread.assert_not_awaited()


class TestFeedbackGate:
    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            (
                "PUT",
                "/feedback",
                {
                    "sessionId": "bob-session",
                    "feedback": {"forId": "step-1", "value": 1, "threadId": "thread-1"},
                },
            ),
            ("DELETE", "/feedback", {"feedbackId": "feedback-1"}),
        ],
    )
    def test_feedback_mutations_are_403_without_feedback_capability(
        self, client, data_layer, grants_active, patch_layer, method, path, payload
    ):
        as_user(BOB)
        data_layer.feedback_authorization_supported = False

        response = client.request(method, path, json=payload)

        assert response.status_code == 403
        data_layer.upsert_feedback.assert_not_awaited()
        data_layer.delete_feedback.assert_not_awaited()
        data_layer.get_feedback_thread_id.assert_not_awaited()

    def _put(self, client, sender=ALICE, feedback=None, session_id="session-1"):
        return client.put(
            "/feedback",
            json={
                "sessionId": session_id,
                "feedback": feedback
                or {"forId": "step-1", "value": 1, "threadId": "thread-1"},
            },
        )

    def test_update_denies_a_session_of_another_principal(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(BOB, "thread-1"),
        ):
            response = self._put(client)

        assert response.status_code == 403
        data_layer.upsert_feedback.assert_not_awaited()

    def test_update_denies_a_missing_session(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True

        with patch("chainlit.session.WebsocketSession.get_by_id", return_value=None):
            response = self._put(client)

        assert response.status_code == 403
        data_layer.upsert_feedback.assert_not_awaited()

    def test_update_denies_a_feedback_without_a_thread(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(ALICE, "thread-1"),
        ):
            response = self._put(
                client, feedback={"forId": "step-1", "value": 1, "threadId": None}
            )

        assert response.status_code == 404
        data_layer.upsert_feedback.assert_not_awaited()

    def test_update_denies_a_thread_the_caller_does_not_own(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=BOB.id)

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(ALICE, "thread-1"),
        ):
            response = self._put(client)

        assert response.status_code == 404
        data_layer.step_belongs_to_thread.assert_not_awaited()
        data_layer.upsert_feedback.assert_not_awaited()

    def test_update_denies_a_step_outside_the_thread(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.step_belongs_to_thread.return_value = False

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(ALICE, "thread-1"),
        ):
            response = self._put(client)

        assert response.status_code == 404
        data_layer.upsert_feedback.assert_not_awaited()

    @pytest.mark.parametrize(
        "lookup",
        [
            FeedbackThreadLookup(status="found", thread_id="thread-2"),
            FeedbackThreadLookup(status="found", thread_id=None),
            FeedbackThreadLookup(status="error"),
            None,
        ],
    )
    def test_update_denies_an_existing_row_it_does_not_resolve(
        self, client, data_layer, grants_active, patch_layer, lookup
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.step_belongs_to_thread.return_value = True
        data_layer.get_feedback_thread_id.return_value = lookup

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(ALICE, "thread-1"),
        ):
            response = self._put(
                client,
                feedback={
                    "forId": "step-1",
                    "value": 1,
                    "threadId": "thread-1",
                    "id": "feedback-1",
                },
            )

        assert response.status_code == 404
        data_layer.get_feedback_thread_id.assert_awaited_once_with("feedback-1")
        data_layer.upsert_feedback.assert_not_awaited()

    def test_update_allows_an_owner_row_in_the_same_thread(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)
        data_layer.step_belongs_to_thread.return_value = True
        data_layer.get_feedback_thread_id.return_value = FeedbackThreadLookup(
            status="found", thread_id="thread-1"
        )
        data_layer.upsert_feedback.return_value = "feedback-1"

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(ALICE, "thread-1"),
        ):
            response = self._put(
                client,
                feedback={
                    "forId": "step-1",
                    "value": 1,
                    "threadId": "thread-1",
                    "id": "feedback-1",
                },
            )

        assert response.status_code == 200, response.text
        data_layer.upsert_feedback.assert_awaited_once()

    def test_delete_of_a_foreign_feedback_is_a_no_op(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(BOB)
        data_layer.feedback_authorization_supported = True
        data_layer.get_feedback_thread_id.return_value = FeedbackThreadLookup(
            status="found", thread_id="thread-1"
        )
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)

        response = client.request(
            "DELETE", "/feedback", json={"feedbackId": "feedback-1"}
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}
        data_layer.delete_feedback.assert_not_awaited()

    @pytest.mark.parametrize(
        "lookup",
        [
            FeedbackThreadLookup(status="not_found"),
            FeedbackThreadLookup(status="found", thread_id=None),
            FeedbackThreadLookup(status="error"),
        ],
    )
    def test_delete_of_an_unresolvable_feedback_is_a_no_op(
        self, client, data_layer, grants_active, patch_layer, lookup
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_feedback_thread_id.return_value = lookup

        response = client.request(
            "DELETE", "/feedback", json={"feedbackId": "feedback-1"}
        )

        assert response.status_code == 200
        data_layer.delete_feedback.assert_not_awaited()

    def test_delete_of_the_owners_feedback_is_allowed(
        self, client, data_layer, grants_active, patch_layer
    ):
        as_user(ALICE)
        data_layer.feedback_authorization_supported = True
        data_layer.get_feedback_thread_id.return_value = FeedbackThreadLookup(
            status="found", thread_id="thread-1"
        )
        data_layer.get_thread_owner.return_value = ThreadOwner(user_id=ALICE.id)

        response = client.request(
            "DELETE", "/feedback", json={"feedbackId": "feedback-1"}
        )

        assert response.status_code == 200
        data_layer.delete_feedback.assert_awaited_once_with("feedback-1")

    def test_feedback_writes_are_untouched_while_the_flag_is_off(
        self, client, data_layer, patch_layer
    ):
        as_user(ALICE)
        data_layer.upsert_feedback.return_value = "feedback-1"

        response = client.put(
            "/feedback",
            json={
                "sessionId": "alice-session",
                "feedback": {"forId": "step-1", "value": 1, "threadId": "thread-1"},
            },
        )

        assert response.status_code == 200
        data_layer.upsert_feedback.assert_awaited_once()
        data_layer.get_feedback_thread_id.assert_not_awaited()
        data_layer.step_belongs_to_thread.assert_not_awaited()


SQL_SCHEMA = [
    """
    CREATE TABLE users (
        "id" UUID PRIMARY KEY,
        "identifier" TEXT NOT NULL UNIQUE,
        "metadata" JSONB NOT NULL,
        "createdAt" TEXT
    );
    """,
    """
    CREATE TABLE threads (
        "id" UUID PRIMARY KEY,
        "createdAt" TEXT,
        "name" TEXT,
        "userId" UUID,
        "userIdentifier" TEXT,
        "tags" TEXT[],
        "metadata" JSONB NOT NULL DEFAULT '{}'
    );
    """,
    """
    CREATE TABLE steps (
        "id" UUID PRIMARY KEY,
        "name" TEXT NOT NULL,
        "type" TEXT NOT NULL,
        "threadId" UUID NOT NULL,
        "parentId" UUID,
        "disableFeedback" BOOLEAN NOT NULL,
        "streaming" BOOLEAN NOT NULL,
        "waitForAnswer" BOOLEAN,
        "isError" BOOLEAN,
        "metadata" JSONB,
        "tags" TEXT[],
        "input" TEXT,
        "output" TEXT,
        "createdAt" TEXT,
        "start" TEXT,
        "end" TEXT,
        "generation" JSONB,
        "showInput" TEXT,
        "language" TEXT,
        "indent" INT
    );
    """,
    """
    CREATE TABLE elements (
        "id" UUID PRIMARY KEY,
        "threadId" UUID,
        "type" TEXT,
        "url" TEXT,
        "chainlitKey" TEXT,
        "name" TEXT NOT NULL,
        "display" TEXT,
        "objectKey" TEXT,
        "size" TEXT,
        "page" INT,
        "language" TEXT,
        "forId" UUID,
        "mime" TEXT,
        "props" JSONB DEFAULT '{}'
    );
    """,
    # threadId stays nullable here: legacy rows carry NULL and must be denied.
    """
    CREATE TABLE feedbacks (
        "id" UUID PRIMARY KEY,
        "forId" UUID NOT NULL,
        "threadId" UUID,
        "value" INT NOT NULL,
        "comment" TEXT
    );
    """,
]


@pytest.fixture
async def sql_layer(tmp_path: Path):
    conninfo = f"sqlite+aiosqlite:///{tmp_path / 'tenancy.sqlite'}"
    engine = create_async_engine(conninfo)
    async with engine.begin() as conn:
        for statement in SQL_SCHEMA:
            await conn.execute(text(statement))
    await engine.dispose()
    layer = SQLAlchemyDataLayer(conninfo)
    yield layer
    await layer.close()


@pytest.fixture
def sql_grants_active(monkeypatch, sql_layer):
    """The real SQL layer behind the S1 chokepoint and grant routes."""
    monkeypatch.setattr(config.features, "thread_grants", True)
    monkeypatch.setattr("chainlit.server.require_login", lambda: True)
    with (
        patch("chainlit.server.get_data_layer", return_value=sql_layer),
        patch("chainlit.data.acl.get_data_layer", return_value=sql_layer),
    ):
        yield sql_layer


async def _sql_thread(layer, owner, thread_id="thread-1", step_id="step-1"):
    await layer.update_thread(thread_id, user_id=owner.id)
    await layer.execute_sql(
        """
        INSERT INTO steps ("id", "name", "type", "threadId", "disableFeedback", "streaming")
        VALUES (:id, 'step', 'assistant_message', :thread_id, false, false)
        """,
        {"id": step_id, "thread_id": thread_id},
    )


async def _sql_feedback(layer, feedback_id, step_id, thread_id, value=1):
    await layer.execute_sql(
        'INSERT INTO feedbacks ("id", "forId", "threadId", "value") '
        "VALUES (:id, :for_id, :thread_id, :value)",
        {
            "id": feedback_id,
            "for_id": step_id,
            "thread_id": thread_id,
            "value": value,
        },
    )


async def _stored_feedback(layer, feedback_id):
    rows = await layer.execute_sql(
        'SELECT * FROM feedbacks WHERE "id" = :id', {"id": feedback_id}
    )
    assert isinstance(rows, list)
    return rows[0] if rows else None


class TestSqlThreadGrants:
    async def test_owner_creates_lists_and_revokes_a_grant(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        as_user(alice)

        created = client.post(
            "/project/thread/thread-1/grants", json={"grantee": "bob"}
        )
        duplicate = client.post(
            "/project/thread/thread-1/grants", json={"grantee": "bob"}
        )
        listed = client.get("/project/thread/thread-1/grants")

        assert created.status_code == 200, created.text
        assert duplicate.status_code == 200
        assert created.json() == duplicate.json() == {"success": True}
        assert listed.status_code == 200
        grants = listed.json()
        assert len(grants) == 1
        assert grants[0]["grantee_user_id"] == bob.id
        assert grants[0]["grantee_identifier"] == "bob"
        assert grants[0]["grantee_user_created_at"] == bob.createdAt
        assert grants[0]["role"] == "read"
        assert listed.headers["cache-control"] == "private, no-store"

        revoked = client.delete(f"/project/thread/thread-1/grants/{bob.id}")
        assert revoked.status_code == 200
        assert client.get("/project/thread/thread-1/grants").json() == []
        assert await layer.get_thread_grant("thread-1", bob.id) is None

    async def test_self_unknown_and_foreign_grants_are_refused(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice)
        as_user(alice)

        own = client.post("/project/thread/thread-1/grants", json={"grantee": "alice"})
        unknown = client.post(
            "/project/thread/thread-1/grants", json={"grantee": "never-seen"}
        )
        assert own.status_code == 400
        assert unknown.status_code == 200
        assert unknown.json() == {"success": True}
        assert await layer.list_thread_grants("thread-1") == []

        as_user(bob)
        foreign = client.post(
            "/project/thread/thread-1/grants", json={"grantee": "bob"}
        )
        assert foreign.status_code == 404
        assert await layer.list_thread_grants("thread-1") == []

    async def test_grant_cap_is_enforced(self, client, sql_grants_active):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice)
        for index in range(MAX_THREAD_GRANTS_PER_THREAD):
            await layer.create_thread_grant(
                ThreadGrant(
                    thread_id="thread-1",
                    grantee_user_id=f"grantee-{index}",
                    grantee_user_created_at="2026-01-01T00:00:00Z",
                    grantee_identifier=f"grantee-{index}",
                    granted_by_user_id=alice.id,
                    granted_at="2026-01-01T00:00:00Z",
                )
            )
        as_user(alice)

        response = client.post(
            "/project/thread/thread-1/grants", json={"grantee": "bob"}
        )

        assert response.status_code == 409
        assert await layer.get_thread_grant("thread-1", bob.id) is None

    async def test_grantee_reads_the_share_route_until_revoked(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        carol = await layer.create_user(User(identifier="carol"))
        assert alice
        assert bob
        assert carol
        await _sql_thread(layer, alice)
        await layer.create_thread_grant(
            ThreadGrant(
                thread_id="thread-1",
                grantee_user_id=bob.id,
                grantee_user_created_at=bob.createdAt,
                grantee_identifier=bob.identifier,
                granted_by_user_id=alice.id,
                granted_at="2026-01-01T00:00:00Z",
            )
        )

        as_user(bob)
        granted = client.get("/project/share/thread-1")
        assert granted.status_code == 200, granted.text
        body = granted.json()
        assert body["userId"] is None
        assert body["userIdentifier"] is None
        assert "chat_settings" not in body
        for key in ("PK", "SK", "UserThreadPK", "UserThreadSK"):
            assert key not in body

        as_user(carol)
        assert client.get("/project/share/thread-1").status_code == 404

        as_user(alice)
        assert (
            client.delete(f"/project/thread/thread-1/grants/{bob.id}").status_code
            == 200
        )

        as_user(bob)
        assert client.get("/project/share/thread-1").status_code == 404

        # An anonymous public link keeps working after the revoke.
        await layer.update_thread("thread-1", metadata={"is_shared": True})
        app.dependency_overrides.pop(get_current_user, None)
        assert client.get("/project/share/thread-1").status_code == 200

    @pytest.mark.parametrize(
        "grant_mutation",
        [
            {"grantee_user_id": "stale-bob-id"},
            {"grantee_user_created_at": "2020-01-01T00:00:00Z"},
            {"grantee_user_created_at": ""},
        ],
    )
    async def test_grants_with_a_wrong_identity_or_generation_are_denied(
        self, client, sql_grants_active, grant_mutation
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice)
        values = {
            "thread_id": "thread-1",
            "grantee_user_id": bob.id,
            "grantee_user_created_at": bob.createdAt,
            "grantee_identifier": "bob",
            "granted_by_user_id": alice.id,
            "granted_at": "2026-01-01T00:00:00Z",
            **grant_mutation,
        }
        await layer.create_thread_grant(ThreadGrant(**values))

        as_user(bob)
        assert client.get("/project/share/thread-1").status_code == 404

    async def test_a_write_role_grant_is_denied(self, client, sql_grants_active):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice)
        await layer.create_thread_grant(
            ThreadGrant(
                thread_id="thread-1",
                grantee_user_id=bob.id,
                grantee_user_created_at=bob.createdAt,
                grantee_identifier="bob",
                granted_by_user_id=alice.id,
                granted_at="2026-01-01T00:00:00Z",
                role="write",
            )
        )

        as_user(bob)
        assert client.get("/project/share/thread-1").status_code == 404

    async def test_grant_creation_never_logs_the_grantee(
        self, client, sql_grants_active, caplog: pytest.LogCaptureFixture
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        assert alice
        await _sql_thread(layer, alice)
        layer.show_logger = True
        as_user(alice)
        caplog.clear()

        with caplog.at_level(logging.INFO):
            response = client.post(
                "/project/thread/thread-1/grants",
                json={"grantee": "quiet-grantee@example.com"},
            )
            rename = client.put(
                "/project/thread", json={"threadId": "thread-1", "name": "renamed"}
            )

        assert response.status_code == 200, response.text
        assert rename.status_code == 200, rename.text
        assert "quiet-grantee@example.com" not in caplog.text
        assert alice.id not in caplog.text


class TestSqlFeedbackH1:
    async def _attacker_put(self, client, layer, bob, feedback):
        as_user(bob)
        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(bob, "thread-2"),
        ):
            return client.put(
                "/feedback", json={"sessionId": "session-2", "feedback": feedback}
            )

    async def test_a_victim_feedback_id_is_denied_and_unchanged(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        await _sql_thread(layer, bob, thread_id="thread-2", step_id="step-2")
        await _sql_feedback(layer, "victim-feedback", "step-1", "thread-1", value=1)

        response = await self._attacker_put(
            client,
            layer,
            bob,
            {
                "id": "victim-feedback",
                "forId": "step-2",
                "value": 0,
                "threadId": "thread-2",
            },
        )

        assert response.status_code == 404
        assert (await _stored_feedback(layer, "victim-feedback"))["threadId"] == (
            "thread-1"
        )
        assert (await _stored_feedback(layer, "victim-feedback"))["value"] == 1

    async def test_a_victim_step_is_denied(self, client, sql_grants_active):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        await _sql_thread(layer, bob, thread_id="thread-2", step_id="step-2")
        await _sql_feedback(layer, "victim-feedback", "step-1", "thread-1", value=1)

        response = await self._attacker_put(
            client,
            layer,
            bob,
            {"forId": "step-1", "value": 0, "threadId": "thread-2"},
        )

        assert response.status_code == 404
        assert (await _stored_feedback(layer, "victim-feedback"))["value"] == 1

    async def test_a_legacy_null_thread_row_is_denied(self, client, sql_grants_active):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        await _sql_thread(layer, bob, thread_id="thread-2", step_id="step-2")
        await _sql_feedback(layer, "legacy-feedback", "step-1", None, value=1)

        # A NULL thread id is invisible to the thread payload, so the attack
        # surface is the client-supplied id: the lookup must find it and deny.
        response = await self._attacker_put(
            client,
            layer,
            bob,
            {
                "id": "legacy-feedback",
                "forId": "step-2",
                "value": 0,
                "threadId": "thread-2",
            },
        )

        assert response.status_code == 404
        assert (await _stored_feedback(layer, "legacy-feedback"))["value"] == 1

    async def test_a_lookup_error_is_denied(self, client, sql_grants_active):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        await _sql_thread(layer, bob, thread_id="thread-2", step_id="step-2")
        await _sql_feedback(layer, "victim-feedback", "step-1", "thread-1", value=1)
        await layer.execute_sql("ALTER TABLE feedbacks RENAME TO feedbacks_old", {})

        response = await self._attacker_put(
            client,
            layer,
            bob,
            {
                "id": "victim-feedback",
                "forId": "step-2",
                "value": 0,
                "threadId": "thread-2",
            },
        )

        assert response.status_code == 404
        rows = await layer.execute_sql(
            'SELECT * FROM feedbacks_old WHERE "id" = :id',
            {"id": "victim-feedback"},
        )
        assert isinstance(rows, list)
        assert rows[0]["value"] == 1

    async def test_the_owner_can_write_their_own_feedback(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        assert alice
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        as_user(alice)

        with patch(
            "chainlit.session.WebsocketSession.get_by_id",
            return_value=_websocket_session(alice, "thread-1"),
        ):
            response = client.put(
                "/feedback",
                json={
                    "sessionId": "alice-session",
                    "feedback": {
                        "forId": "step-1",
                        "value": 1,
                        "threadId": "thread-1",
                    },
                },
            )

        assert response.status_code == 200, response.text
        assert (await _stored_feedback(layer, response.json()["feedbackId"]))[
            "threadId"
        ] == "thread-1"

    async def test_delete_of_a_legacy_null_row_is_a_no_op(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        assert alice
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        await _sql_feedback(layer, "legacy-feedback", "step-1", None, value=1)
        as_user(alice)

        response = client.request(
            "DELETE", "/feedback", json={"feedbackId": "legacy-feedback"}
        )

        assert response.status_code == 200
        assert (await _stored_feedback(layer, "legacy-feedback"))["value"] == 1

    async def test_delete_of_a_foreign_feedback_is_a_no_op(
        self, client, sql_grants_active
    ):
        layer = sql_grants_active
        alice = await layer.create_user(User(identifier="alice"))
        bob = await layer.create_user(User(identifier="bob"))
        assert alice
        assert bob
        await _sql_thread(layer, alice, thread_id="thread-1", step_id="step-1")
        await _sql_feedback(layer, "victim-feedback", "step-1", "thread-1", value=1)
        as_user(bob)

        response = client.request(
            "DELETE", "/feedback", json={"feedbackId": "victim-feedback"}
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}
        assert (await _stored_feedback(layer, "victim-feedback"))["value"] == 1
