from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from chainlit.socket import _mark_message_once, restore_existing_session
from chainlit.user import PersistedUser, User

CANONICAL_MESSAGE_ID = "12345678-1234-4234-8234-123456789abc"
MESSAGE_ID_SPELLINGS = [
    CANONICAL_MESSAGE_ID,
    CANONICAL_MESSAGE_ID.upper(),
    f"{{{CANONICAL_MESSAGE_ID}}}",
    CANONICAL_MESSAGE_ID.replace("-", ""),
    f"urn:uuid:{CANONICAL_MESSAGE_ID}",
]


def message_payload(message_id):
    return {"message": {"id": message_id, "output": "hi"}}


def session_with_seen_ids():
    return SimpleNamespace(processed_message_ids=set())


@pytest.mark.parametrize("message_id", MESSAGE_ID_SPELLINGS)
def test_valid_uuid_spellings_share_one_idempotency_record(message_id):
    session = session_with_seen_ids()

    assert _mark_message_once(session, message_payload(CANONICAL_MESSAGE_ID)) is True
    assert _mark_message_once(session, message_payload(message_id)) is False
    assert session.processed_message_ids == {CANONICAL_MESSAGE_ID}


def test_identical_valid_uuid_replay_is_ignored():
    session = session_with_seen_ids()
    payload = message_payload(CANONICAL_MESSAGE_ID)

    assert _mark_message_once(session, payload) is True
    assert _mark_message_once(session, payload) is False


@pytest.mark.parametrize(
    "message_id",
    [
        "not-a-uuid",
        "12345678-1234-1234-8234-123456789abc",
        f"{CANONICAL_MESSAGE_ID}\x00",
    ],
)
def test_invalid_or_non_v4_ids_keep_emitter_rejection_contract(message_id):
    session = session_with_seen_ids()
    payload = message_payload(message_id)

    assert _mark_message_once(session, payload) is True
    assert _mark_message_once(session, payload) is True
    assert session.processed_message_ids == set()


CREATED_AT = "2026-01-01T00:00:00Z"


def persisted_user(identifier="alice", user_id="user-1", created_at=CREATED_AT):
    return PersistedUser(
        id=user_id,
        createdAt=created_at,
        identifier=identifier,
    )


def restore_session(session_user, authenticated_user):
    session = SimpleNamespace(user=session_user, restore=Mock())
    with patch("chainlit.socket.WebsocketSession.get_by_id", return_value=session):
        result = restore_existing_session(
            "new-sid",
            "session-id",
            Mock(),
            Mock(),
            {},
            user=authenticated_user,
        )
    return result, session


def test_persisted_session_restores_for_same_generation():
    result, session = restore_session(
        persisted_user(),
        persisted_user(),
    )

    assert result is True
    session.restore.assert_called_once_with(new_socket_id="new-sid")


@pytest.mark.parametrize(
    ("user_id", "created_at"),
    [
        ("user-2", CREATED_AT),
        ("user-1", "2026-02-01T00:00:00Z"),
    ],
)
def test_same_identifier_with_different_persisted_generation_is_rejected(
    user_id, created_at
):
    with pytest.raises(ConnectionRefusedError, match="authorization failed"):
        restore_session(
            persisted_user(),
            persisted_user(user_id=user_id, created_at=created_at),
        )


def test_different_identifier_is_rejected():
    with pytest.raises(ConnectionRefusedError, match="authorization failed"):
        restore_session(User(identifier="alice"), User(identifier="bob"))


def test_non_persisted_users_fall_back_to_identifier_matching():
    result, session = restore_session(
        User(identifier="alice"), User(identifier="alice")
    )

    assert result is True
    session.restore.assert_called_once_with(new_socket_id="new-sid")


@pytest.mark.parametrize(
    ("session_user", "authenticated_user"),
    [
        (persisted_user(), User(identifier="alice")),
        (User(identifier="alice"), persisted_user()),
    ],
)
def test_mixed_persisted_users_fall_back_to_identifier_matching(
    session_user, authenticated_user
):
    result, session = restore_session(session_user, authenticated_user)

    assert result is True
    session.restore.assert_called_once_with(new_socket_id="new-sid")


def test_no_login_session_restores_without_a_user():
    result, session = restore_session(None, None)

    assert result is True
    session.restore.assert_called_once_with(new_socket_id="new-sid")


def test_authenticated_user_cannot_restore_no_login_session():
    with pytest.raises(ConnectionRefusedError, match="authorization failed"):
        restore_session(None, User(identifier="alice"))
