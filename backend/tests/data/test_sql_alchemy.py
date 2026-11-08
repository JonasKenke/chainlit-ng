import json
import uuid
from pathlib import Path
from typing import Literal, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit import User
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.element import Text
from chainlit.types import Feedback, Pagination, ThreadFilter


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


async def test_get_thread(test_user: User, data_layer: SQLAlchemyDataLayer):
    persisted_user = await data_layer.create_user(test_user)
    assert persisted_user

    await data_layer.update_thread("test_thread")
    result = await data_layer.get_thread("test_thread")
    assert result is not None

    result = await data_layer.get_thread("nonexisting_thread")
    assert result is None


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
