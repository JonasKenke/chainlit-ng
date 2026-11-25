import json
import logging
import uuid
from pathlib import Path
from typing import Literal, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit import User
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.element import Text
from chainlit.message import MessageBase
from chainlit.thread_metadata import public_thread
from chainlit.types import Feedback, Pagination, ThreadFilter, ThreadGrant


@pytest.fixture
async def data_layer(mock_storage_client: BaseStorageClient, tmp_path: Path):
    db_file = tmp_path / "test_db.sqlite"
    conninfo = f"sqlite+aiosqlite:///{db_file}"

    # Create async engine
    engine = create_async_engine(conninfo)

    # Execute initialization statements
    # Ref: https://docs.chainlit.io/data-persistence/custom#sql-alchemy-data-layer
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE users (
                    "id" UUID PRIMARY KEY,
                    "identifier" TEXT NOT NULL UNIQUE,
                    "metadata" JSONB NOT NULL,
                    "createdAt" TEXT
                );
        """
            )
        )

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    "id" UUID PRIMARY KEY,
                    "createdAt" TEXT,
                    "name" TEXT,
                    "userId" UUID,
                    "userIdentifier" TEXT,
                    "tags" TEXT[],
                    "metadata" JSONB NOT NULL DEFAULT '{}',
                    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
                );
        """
            )
        )

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS steps (
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
        """
            )
        )

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS elements (
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
        """
            )
        )

        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS feedbacks (
                    "id" UUID PRIMARY KEY,
                    "forId" UUID NOT NULL,
                    "threadId" UUID NOT NULL,
                    "value" INT NOT NULL,
                    "comment" TEXT
                );
        """
            )
        )

    # Create SQLAlchemyDataLayer instance
    data_layer = SQLAlchemyDataLayer(conninfo, storage_provider=mock_storage_client)

    return data_layer


async def test_create_and_get_element(
    mock_chainlit_context, data_layer: SQLAlchemyDataLayer
):
    async with mock_chainlit_context:
        text_element = Text(
            id=str(uuid.uuid4()),
            name="test.txt",
            mime="text/plain",
            content="test content",
            for_id="test_step_id",
        )

        # Needs context because of wrapper in utils.py
        await data_layer.create_element(text_element)

    retrieved_element = await data_layer.get_element(
        text_element.thread_id, text_element.id
    )
    assert retrieved_element is not None
    assert retrieved_element["id"] == text_element.id
    assert retrieved_element["name"] == text_element.name
    assert retrieved_element["mime"] == text_element.mime
    # The 'content' field is not part of the ElementDict, so we remove this assertion


async def test_get_current_timestamp(data_layer: SQLAlchemyDataLayer):
    timestamp = await data_layer.get_current_timestamp()
    assert isinstance(timestamp, str)


async def test_get_user(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    fetched_user = await data_layer.get_user(persisted_user.identifier)

    assert fetched_user
    assert fetched_user.createdAt == persisted_user.createdAt
    assert fetched_user.id == persisted_user.id

    nonexistent_user = await data_layer.get_user("nonexistent")
    assert nonexistent_user is None


async def test_get_user_for_acl_does_not_log_the_identifier(
    test_user: User, data_layer: SQLAlchemyDataLayer, caplog: pytest.LogCaptureFixture
):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user
    data_layer.show_logger = True
    caplog.clear()

    with caplog.at_level(logging.INFO):
        fetched_user = await data_layer.get_user_for_acl(persisted_user.identifier)

    assert fetched_user is not None
    assert fetched_user.id == persisted_user.id
    assert persisted_user.identifier not in caplog.text


async def test_get_user_for_acl_db_error_does_not_log_the_identifier(
    data_layer: SQLAlchemyDataLayer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    identifier = "acl-db-error@example.com"

    class FailingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def begin(self):
            return None

        async def execute(self, *args, **kwargs):
            raise SQLAlchemyError(
                f"boom [parameters: {{'identifier': '{identifier}'}}]"
            )

        async def rollback(self):
            return None

    monkeypatch.setattr(data_layer, "async_session", lambda: FailingSession())
    caplog.clear()

    with caplog.at_level(logging.INFO):
        user = await data_layer.get_user_for_acl(identifier)

    assert user is None
    assert "quiet lookup failed" in caplog.text
    assert identifier not in caplog.text


async def test_create_user(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)

    assert persisted_user
    assert persisted_user.identifier == test_user.identifier
    assert persisted_user.createdAt
    assert persisted_user.id

    # Assert id is valid uuid
    assert uuid.UUID(persisted_user.id)


async def test_update_thread(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    await data_layer.update_thread("test_thread")


async def test_get_thread_author(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    await data_layer.update_thread("test_thread", user_id=persisted_user.id)
    author = await data_layer.get_thread_author("test_thread")

    assert author == persisted_user.identifier


async def test_get_thread_owner(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    await data_layer.update_thread("test_thread", user_id=persisted_user.id)
    owner = await data_layer.get_thread_owner("test_thread")

    assert owner is not None
    assert owner.user_id == persisted_user.id
    assert owner.identifier == persisted_user.identifier

    await data_layer.update_thread("ownerless_thread")
    ownerless = await data_layer.get_thread_owner("ownerless_thread")
    assert ownerless is not None
    assert ownerless.user_id is None
    assert ownerless.identifier is None

    assert await data_layer.get_thread_owner("nonexisting_thread") is None


async def test_get_thread(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    await data_layer.update_thread("test_thread")
    result = await data_layer.get_thread("test_thread")
    assert result is not None

    result = await data_layer.get_thread("nonexisting_thread")
    assert result is None


async def test_reload_keeps_elements_position(
    data_layer: SQLAlchemyDataLayer, mock_chainlit_context
):
    """SQLite returns step metadata as a JSON string; the browser must get an object."""
    thread_id = "thread_elements_position"
    step_id = str(uuid.uuid4())
    await data_layer.update_thread(thread_id)
    await data_layer.execute_sql(
        query="""
            INSERT INTO steps (
                "id", "name", "type", "threadId", "disableFeedback",
                "streaming", "output", "createdAt", "metadata"
            ) VALUES (
                :id, :name, :type, :thread_id, :disable_feedback,
                :streaming, :output, :created_at, :metadata
            )
        """,
        parameters={
            "id": step_id,
            "name": "assistant",
            "type": "assistant_message",
            "thread_id": thread_id,
            "disable_feedback": False,
            "streaming": False,
            "output": "Chart attached",
            "created_at": "2026-11-09T00:00:00Z",
            "metadata": json.dumps({"elementsPosition": "above"}),
        },
    )

    thread = await data_layer.get_thread(thread_id)
    assert thread is not None
    reloaded = public_thread(thread, include_settings=True)
    assert reloaded is not None
    step = reloaded["steps"][0]
    assert step["metadata"] == {"elementsPosition": "above"}

    async with mock_chainlit_context:
        restored = MessageBase.from_dict(step)
    assert restored.to_dict()["metadata"] == {"elementsPosition": "above"}


async def test_delete_thread(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    await data_layer.update_thread("test_thread", "test_user")
    await data_layer.delete_thread("test_thread")
    thread = await data_layer.get_thread("test_thread")
    assert thread is None


async def test_list_threads_applies_negative_feedback_filter(
    test_user: User, data_layer: SQLAlchemyDataLayer, mock_chainlit_context
):
    user = await data_layer.create_user(test_user)
    other_user = await data_layer.create_user(User(identifier="other_feedback_user"))
    assert user
    assert other_user

    threads: list[tuple[str, Literal[0, 1] | None, str]] = [
        ("negative", 0, user.id),
        ("positive", 1, user.id),
        ("unrated", None, user.id),
        ("other_user", 0, other_user.id),
    ]

    async with mock_chainlit_context:
        for thread_id, feedback_value, owner_id in threads:
            await data_layer.update_thread(thread_id, user_id=owner_id)
            step_id = f"{thread_id}_step"
            await data_layer.execute_sql(
                query="""
                    INSERT INTO steps (
                        "id", "threadId", "name", "type", "disableFeedback",
                        "streaming", "output", "createdAt"
                    ) VALUES (
                        :id, :thread_id, :name, :type, :disable_feedback,
                        :streaming, :output, :created_at
                    )
                """,
                parameters={
                    "id": step_id,
                    "thread_id": thread_id,
                    "name": "Assistant",
                    "type": "assistant_message",
                    "disable_feedback": False,
                    "streaming": False,
                    "output": "Matching answer",
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
            if feedback_value is not None:
                await data_layer.upsert_feedback(
                    Feedback(
                        forId=step_id,
                        threadId=thread_id,
                        value=feedback_value,
                    )
                )

        response = await data_layer.list_threads(
            Pagination(first=10), ThreadFilter(userId=user.id, feedback=0)
        )

    assert [thread["id"] for thread in response.data] == ["negative"]


async def _get_thread_metadata_raw(
    data_layer: SQLAlchemyDataLayer, thread_id: str
) -> str | None:
    result = await data_layer.execute_sql(
        query='SELECT "metadata" FROM threads WHERE "id" = :id',
        parameters={"id": thread_id},
    )
    if isinstance(result, list) and result:
        return result[0].get("metadata")
    return None


async def test_update_thread_without_metadata_stores_empty_dict(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread("thread_no_meta")

    raw = await _get_thread_metadata_raw(data_layer, "thread_no_meta")
    assert raw is not None
    assert json.loads(raw) == {}


async def test_update_thread_with_explicit_metadata(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread(
        "thread_meta", metadata={"key": "value", "count": 42}
    )

    raw = await _get_thread_metadata_raw(data_layer, "thread_meta")
    assert raw is not None
    assert json.loads(raw) == {"key": "value", "count": 42}


async def test_update_thread_with_empty_metadata(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread("thread_empty_meta", metadata={})

    raw = await _get_thread_metadata_raw(data_layer, "thread_empty_meta")
    assert raw is not None
    assert json.loads(raw) == {}


async def test_update_thread_preserves_metadata_on_noop_call(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread("thread_preserve", metadata={"important": "data"})

    await data_layer.update_thread("thread_preserve")

    raw = await _get_thread_metadata_raw(data_layer, "thread_preserve")
    assert raw is not None
    assert json.loads(raw) == {"important": "data"}


async def test_update_thread_merges_metadata(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread(
        "thread_merge", metadata={"existing": "old", "keep": "me"}
    )

    await data_layer.update_thread(
        "thread_merge", metadata={"existing": "new", "added": "field"}
    )

    raw = await _get_thread_metadata_raw(data_layer, "thread_merge")
    assert raw is not None
    merged = json.loads(raw)
    assert merged == {"existing": "new", "keep": "me", "added": "field"}


async def test_update_thread_deletes_metadata_keys_via_none(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread(
        "thread_delete_key", metadata={"a": 1, "b": 2, "c": 3}
    )

    await data_layer.update_thread("thread_delete_key", metadata={"b": None})

    raw = await _get_thread_metadata_raw(data_layer, "thread_delete_key")
    assert raw is not None
    result = json.loads(raw)
    assert result == {"a": 1, "c": 3}


async def test_update_thread_name_update_preserves_metadata(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread("thread_name_meta", metadata={"saved": True})

    await data_layer.update_thread("thread_name_meta", name="New Name")

    raw = await _get_thread_metadata_raw(data_layer, "thread_name_meta")
    assert raw is not None
    assert json.loads(raw) == {"saved": True}

    result = await data_layer.execute_sql(
        query='SELECT "name" FROM threads WHERE "id" = :id',
        parameters={"id": "thread_name_meta"},
    )
    assert isinstance(result, list)
    assert result
    assert result[0]["name"] == "New Name"


async def _seed_thread_with_children(
    data_layer: SQLAlchemyDataLayer, user_id: str, prefix: str
):
    for index in range(2):
        thread_id = f"{prefix}_thread_{index}"
        step_id = f"{prefix}_step_{index}"
        await data_layer.update_thread(thread_id, user_id=user_id)
        await data_layer.execute_sql(
            query="""
                INSERT INTO steps (
                    "id", "threadId", "name", "type", "disableFeedback", "streaming"
                ) VALUES (
                    :id, :thread_id, :name, :type, :disable_feedback, :streaming
                )
            """,
            parameters={
                "id": step_id,
                "thread_id": thread_id,
                "name": "Assistant",
                "type": "assistant_message",
                "disable_feedback": False,
                "streaming": False,
            },
        )
        await data_layer.execute_sql(
            query="""
                INSERT INTO elements ("id", "threadId", "forId", "name", "objectKey")
                VALUES (:id, :thread_id, :for_id, :name, :object_key)
            """,
            parameters={
                "id": f"{prefix}_element_{index}",
                "thread_id": thread_id,
                "for_id": step_id,
                "name": "attachment.txt",
                "object_key": f"{prefix}-object-{index}",
            },
        )
        await data_layer.upsert_feedback(
            Feedback(forId=step_id, threadId=thread_id, value=1)
        )


async def test_delete_all_threads_only_deletes_target_user(
    data_layer: SQLAlchemyDataLayer, mock_storage_client: BaseStorageClient
):
    target = await data_layer.create_user(User(identifier="delete_all_target"))
    other = await data_layer.create_user(User(identifier="delete_all_other"))
    assert target
    assert other

    await _seed_thread_with_children(data_layer, target.id, "target")
    await _seed_thread_with_children(data_layer, other.id, "other")

    deleted = await data_layer.delete_all_threads(target.id)

    delete_file = cast(AsyncMock, mock_storage_client.delete_file)
    assert deleted == 2
    assert {call.kwargs["object_key"] for call in delete_file.await_args_list} == {
        "target-object-0",
        "target-object-1",
    }

    assert await data_layer.delete_all_threads(target.id) == 0
    assert delete_file.await_count == 2

    target_threads = await data_layer.execute_sql(
        query='SELECT "id" FROM threads WHERE "userId" = :user_id',
        parameters={"user_id": target.id},
    )
    assert target_threads == []

    other_threads = await data_layer.execute_sql(
        query='SELECT "id" FROM threads WHERE "userId" = :user_id',
        parameters={"user_id": other.id},
    )
    assert isinstance(other_threads, list)
    assert sorted(thread["id"] for thread in other_threads) == [
        "other_thread_0",
        "other_thread_1",
    ]

    for table in ("steps", "elements", "feedbacks"):
        column = "forId" if table == "feedbacks" else "threadId"
        target_rows = await data_layer.execute_sql(
            query=f'SELECT "id" FROM {table} WHERE "{column}" LIKE :prefix',
            parameters={"prefix": "target%"},
        )
        assert target_rows == []

        other_rows = await data_layer.execute_sql(
            query=f'SELECT "id" FROM {table} WHERE "{column}" LIKE :prefix',
            parameters={"prefix": "other%"},
        )
        assert isinstance(other_rows, list)
        assert len(other_rows) == 2


async def test_delete_thread_keeps_rows_when_storage_delete_fails(
    data_layer: SQLAlchemyDataLayer, mock_storage_client: BaseStorageClient
):
    target = await data_layer.create_user(User(identifier="delete_thread_failure"))
    assert target

    await _seed_thread_with_children(data_layer, target.id, "target")
    cast(AsyncMock, mock_storage_client.delete_file).return_value = False

    with pytest.raises(RuntimeError, match="storage object"):
        await data_layer.delete_thread("target_thread_0")

    thread = await data_layer.get_thread("target_thread_0")
    assert thread is not None


async def test_delete_all_threads_keeps_rows_when_storage_delete_fails(
    data_layer: SQLAlchemyDataLayer, mock_storage_client: BaseStorageClient
):
    target = await data_layer.create_user(User(identifier="delete_all_failure"))
    assert target

    await _seed_thread_with_children(data_layer, target.id, "target")
    cast(AsyncMock, mock_storage_client.delete_file).return_value = False

    with pytest.raises(RuntimeError, match="storage object"):
        await data_layer.delete_all_threads(target.id)

    rows = await data_layer.execute_sql(
        query='SELECT "id" FROM threads WHERE "userId" = :user_id',
        parameters={"user_id": target.id},
    )
    assert isinstance(rows, list)
    assert len(rows) == 2


async def test_delete_all_threads_raises_when_sql_fails(
    data_layer: SQLAlchemyDataLayer, monkeypatch: pytest.MonkeyPatch
):
    async def failing_execute_sql(*args, **kwargs):
        return None

    monkeypatch.setattr(data_layer, "execute_sql", failing_execute_sql)

    with pytest.raises(RuntimeError, match="delete_all_threads failed"):
        await data_layer.delete_all_threads("some-user")


async def test_list_threads_only_returns_target_user_threads(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="list_alice"))
    bob = await data_layer.create_user(User(identifier="list_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")

    page = await data_layer.list_threads(
        Pagination(first=50), ThreadFilter(userId=alice.id)
    )

    assert {thread["id"] for thread in page.data} == {
        "alice_thread_0",
        "alice_thread_1",
    }


async def test_list_threads_requires_a_user_id(data_layer: SQLAlchemyDataLayer):
    with pytest.raises(ValueError, match="userId is required"):
        await data_layer.list_threads(Pagination(first=50), ThreadFilter())


async def test_list_threads_includes_granted_threads(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="grant_list_alice"))
    bob = await data_layer.create_user(User(identifier="grant_list_bob"))
    carol = await data_layer.create_user(User(identifier="grant_list_carol"))
    assert alice
    assert bob
    assert carol
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")
    await data_layer.create_thread_grant(
        _grant(
            thread_id="alice_thread_0",
            grantee_user_id=bob.id,
            grantee_user_created_at=bob.createdAt,
        )
    )

    granted = await data_layer.list_threads(
        Pagination(first=50),
        ThreadFilter(
            userId=bob.id,
            sharedWithUserId=bob.id,
            sharedWithUserCreatedAt=bob.createdAt,
        ),
    )
    owner_only = await data_layer.list_threads(
        Pagination(first=50), ThreadFilter(userId=bob.id)
    )

    assert {thread["id"] for thread in granted.data} == {
        "alice_thread_0",
        "bob_thread_0",
        "bob_thread_1",
    }
    assert {thread["id"] for thread in owner_only.data} == {
        "bob_thread_0",
        "bob_thread_1",
    }

    non_grantee = await data_layer.list_threads(
        Pagination(first=50),
        ThreadFilter(
            userId=carol.id,
            sharedWithUserId=carol.id,
            sharedWithUserCreatedAt=carol.createdAt,
        ),
    )
    assert non_grantee.data == []


async def test_list_threads_grant_requires_the_matching_generation(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="generation_alice"))
    bob = await data_layer.create_user(User(identifier="generation_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await data_layer.create_thread_grant(
        _grant(
            thread_id="alice_thread_0",
            grantee_user_id=bob.id,
            grantee_user_created_at="2020-01-01T00:00:00Z",
        )
    )

    page = await data_layer.list_threads(
        Pagination(first=50),
        ThreadFilter(
            userId=bob.id,
            sharedWithUserId=bob.id,
            sharedWithUserCreatedAt=bob.createdAt,
        ),
    )

    assert page.data == []


async def test_list_threads_paginates_only_visible_granted_rows(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="cursor_alice"))
    bob = await data_layer.create_user(User(identifier="cursor_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    for index in range(2):
        await data_layer.execute_sql(
            query='UPDATE steps SET "createdAt" = :created_at WHERE "threadId" = :thread_id',
            parameters={
                "created_at": f"2026-01-0{index + 1}T00:00:00Z",
                "thread_id": f"alice_thread_{index}",
            },
        )
        await data_layer.create_thread_grant(
            _grant(
                thread_id=f"alice_thread_{index}",
                grantee_user_id=bob.id,
                grantee_user_created_at=bob.createdAt,
            )
        )

    filters = ThreadFilter(
        userId=bob.id,
        sharedWithUserId=bob.id,
        sharedWithUserCreatedAt=bob.createdAt,
    )
    first_page = await data_layer.list_threads(Pagination(first=1), filters)
    assert len(first_page.data) == 1
    assert first_page.pageInfo.hasNextPage is True
    assert first_page.pageInfo.startCursor == first_page.data[0]["id"]
    assert first_page.pageInfo.endCursor == first_page.data[0]["id"]

    second_page = await data_layer.list_threads(
        Pagination(first=10, cursor=first_page.pageInfo.endCursor), filters
    )
    assert len(second_page.data) == 1
    assert second_page.pageInfo.hasNextPage is False
    assert {
        first_page.data[0]["id"],
        second_page.data[0]["id"],
    } == {"alice_thread_0", "alice_thread_1"}


async def test_list_threads_falls_back_to_owner_only_when_the_table_is_unavailable(
    data_layer: SQLAlchemyDataLayer,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    alice = await data_layer.create_user(User(identifier="fallback_alice"))
    bob = await data_layer.create_user(User(identifier="fallback_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")
    await data_layer.create_thread_grant(
        _grant(
            thread_id="alice_thread_0",
            grantee_user_id=bob.id,
            grantee_user_created_at=bob.createdAt,
        )
    )

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(data_layer, "_ensure_thread_grants_table", unavailable)

    with caplog.at_level(logging.WARNING):
        page = await data_layer.list_threads(
            Pagination(first=50),
            ThreadFilter(
                userId=bob.id,
                sharedWithUserId=bob.id,
                sharedWithUserCreatedAt=bob.createdAt,
            ),
        )

    assert {thread["id"] for thread in page.data} == {
        "bob_thread_0",
        "bob_thread_1",
    }
    assert "listing owned threads only" in caplog.text


async def test_get_element_is_scoped_to_its_thread(data_layer: SQLAlchemyDataLayer):
    alice = await data_layer.create_user(User(identifier="element_alice"))
    bob = await data_layer.create_user(User(identifier="element_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")

    assert await data_layer.get_element("alice_thread_0", "alice_element_0")
    assert await data_layer.get_element("alice_thread_1", "alice_element_0") is None
    assert await data_layer.get_element("bob_thread_0", "alice_element_0") is None


async def test_delete_element_is_scoped_to_its_thread(
    mock_chainlit_context,
    data_layer: SQLAlchemyDataLayer,
    mock_storage_client: BaseStorageClient,
):
    alice = await data_layer.create_user(User(identifier="delete_element_alice"))
    bob = await data_layer.create_user(User(identifier="delete_element_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")
    delete_file = cast(AsyncMock, mock_storage_client.delete_file)

    async with mock_chainlit_context:
        await data_layer.delete_element("alice_element_0", "bob_thread_0")

        assert await data_layer.get_element("alice_thread_0", "alice_element_0")
        delete_file.assert_not_awaited()

        await data_layer.delete_element("alice_element_0", "alice_thread_0")

    assert await data_layer.get_element("alice_thread_0", "alice_element_0") is None
    delete_file.assert_awaited_once_with(object_key="alice-object-0")


async def test_create_element_cannot_overwrite_another_threads_element(
    mock_chainlit_context,
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="overwrite_alice"))
    bob = await data_layer.create_user(User(identifier="overwrite_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")

    async with mock_chainlit_context:
        await data_layer.create_element(
            Text(
                id="alice_element_0",
                name="overwrite.txt",
                mime="text/plain",
                content="overwritten",
                for_id="bob_step_0",
                thread_id="bob_thread_0",
            )
        )

    element = await data_layer.get_element("alice_thread_0", "alice_element_0")
    assert element is not None
    assert element["name"] == "attachment.txt"
    assert await data_layer.get_element("bob_thread_0", "alice_element_0") is None


async def test_favorite_steps_only_returns_target_user_steps(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="fav_alice"))
    bob = await data_layer.create_user(User(identifier="fav_bob"))
    assert alice
    assert bob
    await _seed_thread_with_children(data_layer, alice.id, "alice")
    await _seed_thread_with_children(data_layer, bob.id, "bob")
    for prefix in ("alice", "bob"):
        await data_layer.execute_sql(
            query='UPDATE steps SET "metadata" = :metadata WHERE "id" LIKE :prefix',
            parameters={"metadata": '{"favorite": true}', "prefix": f"{prefix}_step_%"},
        )

    steps = await data_layer.get_favorite_steps(alice.id)

    assert {step["id"] for step in steps} == {"alice_step_0", "alice_step_1"}


async def test_prompts_are_scoped_to_their_owner(data_layer: SQLAlchemyDataLayer):
    alice = await data_layer.create_user(User(identifier="prompt_alice"))
    bob = await data_layer.create_user(User(identifier="prompt_bob"))
    assert alice
    assert bob
    now = "2026-01-01T00:00:00Z"
    for owner, prompt_id in ((alice, "alice-prompt"), (bob, "bob-prompt")):
        await data_layer.create_prompt(
            {
                "id": prompt_id,
                "userId": owner.id,
                "title": prompt_id,
                "content": "secret",
                "isShared": False,
                "createdAt": now,
                "updatedAt": now,
            }
        )

    listed = await data_layer.list_prompts(alice.id)

    assert [prompt["id"] for prompt in listed] == ["alice-prompt"]
    assert await data_layer.delete_prompt("alice-prompt", bob.id) is False
    assert await data_layer.delete_prompt("alice-prompt", alice.id) is True


@pytest.mark.parametrize(
    "payload_template",
    [
        # UNION read of another user's steps (the original F1 repro).
        (
            "x') UNION SELECT 'inj-step','Injected','assistant_message',t.\"id\","
            "NULL,0,NULL,0,'{}',NULL,'',v.\"output\",NULL,NULL,NULL,NULL,NULL,"
            "NULL,NULL,NULL,NULL FROM threads t, steps v "
            'WHERE t."id" = (SELECT "id" FROM threads WHERE "userId" = '
            "'__ATTACKER_ID__') AND v.\"threadId\" = 'victim_thread' --"
        ),
        # Make every step match.
        "x') OR 1=1 --",
        # Stacked statement attempt.
        "x'; DROP TABLE steps; --",
    ],
)
async def test_thread_id_cannot_inject_sql(
    data_layer: SQLAlchemyDataLayer, payload_template: str
):
    attacker = await data_layer.create_user(User(identifier="sqli_attacker"))
    victim = await data_layer.create_user(User(identifier="sqli_victim"))
    assert attacker
    assert victim
    await data_layer.update_thread("victim_thread", user_id=victim.id)
    await data_layer.execute_sql(
        query="""
            INSERT INTO steps
                ("id", "threadId", "name", "type", "output", "disableFeedback", "streaming")
            VALUES
                (:id, :thread_id, :name, :type, :output, 0, 0)
        """,
        parameters={
            "id": "victim-step",
            "thread_id": "victim_thread",
            "name": "victim",
            "type": "assistant_message",
            "output": "VICTIM-SECRET",
        },
    )
    payload = payload_template.replace("__ATTACKER_ID__", attacker.id)
    await data_layer.update_thread(payload, user_id=attacker.id)

    threads = await data_layer.get_all_user_threads(user_id=attacker.id)

    leaked = [
        step.get("output")
        for thread in threads or []
        for step in thread["steps"]
        if step.get("output") == "VICTIM-SECRET"
    ]
    assert leaked == []

    victim_steps = await data_layer.execute_sql(
        query='SELECT COUNT(*) AS count FROM steps WHERE "threadId" = :thread_id',
        parameters={"thread_id": "victim_thread"},
    )
    assert victim_steps == [{"count": 1}]


async def test_element_storage_key_stays_under_the_thread_owner(
    data_layer: SQLAlchemyDataLayer,
    mock_storage_client: BaseStorageClient,
    mock_chainlit_context,
):
    alice = await data_layer.create_user(User(identifier="element_key_alice"))
    bob = await data_layer.create_user(User(identifier="element_key_bob"))
    assert alice
    assert bob
    await data_layer.update_thread("alice-thread", user_id=alice.id)
    element = Text(
        id="element-key-1",
        name="../../bob/overwrite.txt",
        mime="text/plain",
        content="payload",
        thread_id="alice-thread",
        for_id="step-1",
    )

    async with mock_chainlit_context:
        await data_layer.create_element(element)

    upload = cast(AsyncMock, mock_storage_client).upload_file
    object_key = upload.await_args.kwargs["object_key"]
    assert object_key.startswith(f"{alice.id}/{element.id}/")
    assert not object_key.startswith(f"{bob.id}/")


class _FailingSession:
    """Session double whose driver error renders the query parameters."""

    def __init__(self, error: Exception):
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def begin(self):
        return None

    async def execute(self, *args, **kwargs):
        raise self.error

    async def rollback(self):
        return None


def _grant(
    thread_id: str = "thread-1",
    grantee_user_id: str = "bob-id",
    grantee_user_created_at: str = "2026-01-01T00:00:00Z",
    role: str = "read",
) -> ThreadGrant:
    return ThreadGrant(
        thread_id=thread_id,
        grantee_user_id=grantee_user_id,
        grantee_user_created_at=grantee_user_created_at,
        grantee_identifier="bob",
        granted_by_user_id="alice-id",
        granted_at="2026-01-02T00:00:00Z",
        role=role,
    )


async def test_thread_grants_are_a_layer_capability(data_layer: SQLAlchemyDataLayer):
    assert data_layer.thread_grants_supported is True
    assert data_layer.feedback_authorization_supported is True


async def test_thread_grant_crud(data_layer: SQLAlchemyDataLayer):
    await data_layer.create_thread_grant(_grant())

    stored = await data_layer.get_thread_grant("thread-1", "bob-id")
    assert stored == _grant()
    assert await data_layer.list_thread_grants("thread-1") == [_grant()]

    assert await data_layer.delete_thread_grant("thread-1", "bob-id") is True
    assert await data_layer.get_thread_grant("thread-1", "bob-id") is None
    assert await data_layer.list_thread_grants("thread-1") == []
    assert await data_layer.delete_thread_grant("thread-1", "bob-id") is False


async def test_duplicate_thread_grant_is_a_noop(data_layer: SQLAlchemyDataLayer):
    await data_layer.create_thread_grant(_grant())
    await data_layer.create_thread_grant(_grant())

    assert len(await data_layer.list_thread_grants("thread-1")) == 1


async def test_thread_grant_lookup_is_scoped(data_layer: SQLAlchemyDataLayer):
    await data_layer.create_thread_grant(_grant())

    assert await data_layer.get_thread_grant("thread-1", "carol-id") is None
    assert await data_layer.get_thread_grant("thread-2", "bob-id") is None


async def test_thread_grant_lookup_denies_a_non_read_role(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.create_thread_grant(_grant(role="write"))

    assert await data_layer.get_thread_grant("thread-1", "bob-id") is None


async def test_thread_grant_errors_do_not_log_the_grantee(
    data_layer: SQLAlchemyDataLayer,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    identifier = "secret-grantee@example.com"
    await data_layer.create_thread_grant(_grant())
    data_layer.show_logger = True
    monkeypatch.setattr(
        data_layer,
        "async_session",
        lambda: _FailingSession(
            SQLAlchemyError(f"boom [parameters: {{'grantee_user_id': '{identifier}'}}]")
        ),
    )
    caplog.clear()

    with caplog.at_level(logging.INFO):
        assert await data_layer.get_thread_grant("thread-1", identifier) is None
        with pytest.raises(RuntimeError):
            await data_layer.create_thread_grant(_grant(grantee_user_id=identifier))

    assert identifier not in caplog.text
    assert "quiet lookup failed" in caplog.text


async def test_grants_deny_when_the_table_cannot_be_created(
    data_layer: SQLAlchemyDataLayer, monkeypatch: pytest.MonkeyPatch
):
    async def failing_execute_sql(*args, **kwargs):
        return None

    monkeypatch.setattr(data_layer, "execute_sql", failing_execute_sql)

    assert await data_layer.get_thread_grant("thread-1", "bob-id") is None
    assert await data_layer.list_thread_grants("thread-1") == []
    with pytest.raises(RuntimeError):
        await data_layer.create_thread_grant(_grant())


async def test_delete_thread_removes_its_grants(
    data_layer: SQLAlchemyDataLayer, test_user: User
):
    owner = await data_layer.create_user(test_user)
    assert owner
    await data_layer.update_thread("thread-1", user_id=owner.id)
    await data_layer.create_thread_grant(_grant())

    await data_layer.delete_thread("thread-1")

    assert await data_layer.list_thread_grants("thread-1") == []


async def test_delete_all_threads_removes_only_the_owners_grants(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="grants_alice"))
    bob = await data_layer.create_user(User(identifier="grants_bob"))
    assert alice
    assert bob
    await data_layer.update_thread("alice-thread", user_id=alice.id)
    await data_layer.update_thread("bob-thread", user_id=bob.id)
    await data_layer.create_thread_grant(_grant(thread_id="alice-thread"))
    await data_layer.create_thread_grant(_grant(thread_id="bob-thread"))

    deleted = await data_layer.delete_all_threads(alice.id)

    assert deleted >= 1
    assert await data_layer.list_thread_grants("alice-thread") == []
    assert await data_layer.list_thread_grants("bob-thread") == [
        _grant(thread_id="bob-thread")
    ]


async def test_update_thread_without_an_owner_keeps_grants(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.create_thread_grant(_grant())

    await data_layer.update_thread("thread-1", name="renamed")

    assert await data_layer.list_thread_grants("thread-1") == [_grant()]


async def test_update_thread_owner_change_removes_grants(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="grants_owner_alice"))
    bob = await data_layer.create_user(User(identifier="grants_owner_bob"))
    assert alice
    assert bob
    await data_layer.update_thread("thread-1", user_id=alice.id)
    await data_layer.create_thread_grant(_grant())

    await data_layer.update_thread("thread-1", user_id=alice.id)
    assert await data_layer.list_thread_grants("thread-1") == [_grant()]

    await data_layer.update_thread("thread-1", user_id=bob.id)
    assert await data_layer.list_thread_grants("thread-1") == []


async def test_get_feedback_thread_id_is_typed(data_layer: SQLAlchemyDataLayer):
    assert (
        await data_layer.get_feedback_thread_id("missing-feedback")
    ).status == "not_found"

    await data_layer.execute_sql(
        'INSERT INTO feedbacks ("id", "forId", "threadId", "value") VALUES (:id, :for_id, :thread_id, 1)',
        {"id": "feedback-1", "for_id": "step-1", "thread_id": "thread-1"},
    )

    found = await data_layer.get_feedback_thread_id("feedback-1")
    assert found.status == "found"
    assert found.thread_id == "thread-1"


async def test_get_feedback_thread_id_errors_are_typed(
    tmp_path: Path, mock_storage_client: BaseStorageClient
):
    """A legacy row may have a NULL threadId; SQLite needs a nullable table."""
    db_file = tmp_path / "legacy_db.sqlite"
    conninfo = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(conninfo)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE feedbacks (
                    "id" TEXT PRIMARY KEY,
                    "forId" TEXT NOT NULL,
                    "threadId" TEXT,
                    "value" INT NOT NULL
                );
                """
            )
        )
        await conn.execute(
            text(
                'INSERT INTO feedbacks ("id", "forId", "threadId", "value") '
                "VALUES ('legacy-feedback', 'step-1', NULL, 1)"
            )
        )
    await engine.dispose()
    layer = SQLAlchemyDataLayer(conninfo, storage_provider=mock_storage_client)
    try:
        lookup = await layer.get_feedback_thread_id("legacy-feedback")
        assert lookup.status == "found"
        assert lookup.thread_id is None
    finally:
        await layer.close()


async def test_get_feedback_thread_id_failure_is_an_error(
    data_layer: SQLAlchemyDataLayer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        data_layer,
        "async_session",
        lambda: _FailingSession(SQLAlchemyError("boom")),
    )

    assert (await data_layer.get_feedback_thread_id("feedback-1")).status == "error"


async def test_step_belongs_to_thread_is_a_point_check(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.execute_sql(
        """
        INSERT INTO steps ("id", "name", "type", "threadId", "disableFeedback", "streaming")
        VALUES (:id, 'step', 'assistant_message', :thread_id, false, false)
        """,
        {"id": "step-1", "thread_id": "thread-1"},
    )

    assert await data_layer.step_belongs_to_thread("thread-1", "step-1") is True
    assert await data_layer.step_belongs_to_thread("thread-2", "step-1") is False
    assert await data_layer.step_belongs_to_thread("thread-1", "step-2") is False


async def test_step_belongs_to_thread_denies_on_error(
    data_layer: SQLAlchemyDataLayer, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        data_layer,
        "async_session",
        lambda: _FailingSession(SQLAlchemyError("boom")),
    )

    assert await data_layer.step_belongs_to_thread("thread-1", "step-1") is False


async def test_upsert_feedback_does_not_overwrite_another_threads_row(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.upsert_feedback(
        Feedback(id="feedback-1", forId="step-1", threadId="thread-1", value=1)
    )

    await data_layer.upsert_feedback(
        Feedback(id="feedback-1", forId="step-2", threadId="thread-2", value=0)
    )

    row = await data_layer.execute_sql(
        'SELECT * FROM feedbacks WHERE "id" = :id', {"id": "feedback-1"}
    )
    assert isinstance(row, list)
    assert row[0]["threadId"] == "thread-1"
    assert row[0]["forId"] == "step-1"
    assert row[0]["value"] == 1


async def test_upsert_feedback_still_updates_its_own_thread(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.upsert_feedback(
        Feedback(id="feedback-1", forId="step-1", threadId="thread-1", value=1)
    )

    await data_layer.upsert_feedback(
        Feedback(id="feedback-1", forId="step-1", threadId="thread-1", value=0)
    )

    row = await data_layer.execute_sql(
        'SELECT * FROM feedbacks WHERE "id" = :id', {"id": "feedback-1"}
    )
    assert isinstance(row, list)
    assert row[0]["value"] == 0


async def test_grant_reads_deny_on_a_query_failure(
    data_layer: SQLAlchemyDataLayer, monkeypatch: pytest.MonkeyPatch
):
    await data_layer.create_thread_grant(_grant())

    async def failing_execute_sql(*args, **kwargs):
        return None

    monkeypatch.setattr(data_layer, "execute_sql", failing_execute_sql)

    assert await data_layer.get_thread_grant("thread-1", "bob-id") is None
    assert await data_layer.list_thread_grants("thread-1") == []


async def test_revoke_failure_is_not_silent(
    data_layer: SQLAlchemyDataLayer, monkeypatch: pytest.MonkeyPatch
):
    await data_layer.create_thread_grant(_grant())

    async def failing_execute_sql(*args, **kwargs):
        return None

    monkeypatch.setattr(data_layer, "execute_sql", failing_execute_sql)

    with pytest.raises(RuntimeError):
        await data_layer.delete_thread_grant("thread-1", "bob-id")


async def test_delete_thread_works_without_a_grants_table(
    data_layer: SQLAlchemyDataLayer,
):
    await data_layer.update_thread("thread-1")
    data_layer._thread_grants_table_checked = True
    data_layer._thread_grants_table_ready = False

    await data_layer.delete_thread("thread-1")

    assert await data_layer.get_thread("thread-1") is None


async def test_delete_all_threads_works_without_a_grants_table(
    data_layer: SQLAlchemyDataLayer,
):
    alice = await data_layer.create_user(User(identifier="grants_no_table"))
    assert alice
    await data_layer.update_thread("thread-1", user_id=alice.id)
    data_layer._thread_grants_table_checked = True
    data_layer._thread_grants_table_ready = False

    assert await data_layer.delete_all_threads(alice.id) == 1


async def test_execute_sql_raise_on_error_is_additive(data_layer: SQLAlchemyDataLayer):
    assert (
        await data_layer.execute_sql("SELECT * FROM table_that_does_not_exist", {})
        is None
    )

    with pytest.raises(SQLAlchemyError):
        await data_layer.execute_sql(
            "SELECT * FROM table_that_does_not_exist", {}, raise_on_error=True
        )


async def test_create_step_cannot_move_a_step_between_threads(
    mock_chainlit_context, data_layer: SQLAlchemyDataLayer
):
    """A shared step id must not let another thread overwrite or move it."""
    step_id = str(uuid.uuid4())
    original = {
        "id": step_id,
        "name": "original",
        "type": "assistant_message",
        "threadId": "thread-a",
        "disableFeedback": False,
        "streaming": False,
        "output": "original output",
        "createdAt": "2026-01-01T00:00:00Z",
        "metadata": {},
        "generation": {},
    }
    forged = {
        **original,
        "threadId": "thread-b",
        "output": "forged output",
        "type": "user_message",
    }

    async with mock_chainlit_context:
        await data_layer.create_step(original)
        await data_layer.create_step(forged)

    rows = await data_layer.execute_sql(
        'SELECT "threadId", "output" FROM steps WHERE "id" = :id',
        {"id": step_id},
    )
    assert rows == [{"threadId": "thread-a", "output": "original output"}]


async def test_concurrent_unshare_and_rename_keep_the_thread_unshared(
    mock_chainlit_context, data_layer: SQLAlchemyDataLayer
):
    """A stale metadata write must not reverse an unshare."""
    thread_id = "thread-share-race"
    async with mock_chainlit_context:
        await data_layer.update_thread(
            thread_id=thread_id,
            metadata={"is_shared": True, "shared_at": "2026-01-01T00:00:00Z"},
        )

        import asyncio

        await asyncio.gather(
            data_layer.update_thread(
                thread_id=thread_id, metadata={"is_shared": False}
            ),
            data_layer.update_thread(
                thread_id=thread_id, metadata={"note": "concurrent"}
            ),
        )

        stored = await data_layer.get_thread(thread_id)

    assert stored is not None
    raw_metadata = stored.get("metadata")
    metadata = (
        json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
    )
    assert isinstance(metadata, dict)
    assert metadata["is_shared"] is False
    assert metadata.get("note") == "concurrent"


async def test_ssl_require_verifies_certificates_and_hostname(tmp_path):
    import ssl
    from unittest.mock import patch

    captured = {}
    original = create_async_engine

    def capture_engine(*args, **kwargs):
        captured.update(kwargs)
        return original(*args, **kwargs)

    with patch(
        "chainlit.data.sql_alchemy.create_async_engine",
        side_effect=capture_engine,
    ):
        layer = SQLAlchemyDataLayer(
            f"sqlite+aiosqlite:///{tmp_path / 'ssl.sqlite'}", ssl_require=True
        )

    context = captured["connect_args"]["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    await layer.close()


async def test_create_step_creates_the_thread_row(
    mock_chainlit_context, data_layer: SQLAlchemyDataLayer
):
    """The built-in layer self-heals thread ordering for strict callers."""
    step_id = str(uuid.uuid4())
    async with mock_chainlit_context:
        await data_layer.create_step(
            {
                "id": step_id,
                "name": "user",
                "type": "user_message",
                "threadId": "round-trip-thread",
                "disableFeedback": False,
                "streaming": False,
                "output": "hello",
                "createdAt": "2026-01-01T00:00:00Z",
                "metadata": {},
                "generation": {},
            }
        )
        thread = await data_layer.get_thread("round-trip-thread")

    assert thread is not None
    assert thread["steps"][0]["id"] == step_id
