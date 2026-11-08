import json
from copy import deepcopy
from typing import Any, Dict, cast
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from chainlit.data.base import BaseDataLayer
from chainlit.data.chainlit_data_layer import ChainlitDataLayer
from chainlit.data.dynamodb import DynamoDBDataLayer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer


def _record(
    approval_id: str,
    status: str = "pending",
    revision: int = 1,
) -> Dict[str, Any]:
    return {
        "version": 1,
        "revision": revision,
        "id": approval_id,
        "stepId": "step-1",
        "threadId": "thread-1",
        "tool": "tool",
        "title": None,
        "status": status,
        "requestedBy": "user-1",
        "requestedAt": "2026-01-01T00:00:00Z",
        "expiresAt": "2026-01-01T00:05:00Z",
        "editAllowed": False,
        "digestSalt": "salt",
        "argumentsDigest": "hmac-sha256:digest",
        "argumentsPreview": {"amount": 1},
        "decidedBy": None,
        "decidedAt": None,
        "decision": None,
        "editedArgumentsPreview": None,
        "editedArgumentsDigest": None,
        "reason": None,
        "decisionRequestId": None,
        "terminalReason": None,
        "terminalAt": None,
    }


@pytest.fixture
async def sqlalchemy_layer(tmp_path):
    db_file = tmp_path / "approval.sqlite"
    conninfo = f"sqlite+aiosqlite:///{db_file}"
    engine = create_async_engine(conninfo)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE steps (
                    "id" UUID PRIMARY KEY,
                    "name" TEXT,
                    "type" TEXT,
                    "threadId" UUID,
                    "metadata" JSONB
                );
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO steps ("id", "name", "type", "threadId", "metadata")
                VALUES (:id, :name, :type, :thread_id, :metadata);
                """
            ),
            {
                "id": "step-1",
                "name": "tool",
                "type": "tool",
                "thread_id": "thread-1",
                "metadata": json.dumps({"keep": "me"}),
            },
        )
    await engine.dispose()
    layer = SQLAlchemyDataLayer(conninfo)
    yield layer
    await layer.close()


async def _stored(layer: SQLAlchemyDataLayer) -> Dict[str, Any]:
    rows = await layer.execute_sql(
        "SELECT metadata FROM steps WHERE id = :id", {"id": "step-1"}
    )
    assert isinstance(rows, list)
    raw = rows[0]["metadata"]
    return json.loads(raw) if isinstance(raw, str) else raw


async def test_sqlalchemy_conditional_write_proves_rowcount(
    sqlalchemy_layer: SQLAlchemyDataLayer,
):
    pending = _record("approval-1")
    assert (
        await sqlalchemy_layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=pending,
            expected_revision=None,
        )
        is True
    )
    stored = await _stored(sqlalchemy_layer)
    assert stored["approval"]["status"] == "pending"
    assert stored["keep"] == "me"

    approved = _record("approval-1", "approved", 2)
    assert (
        await sqlalchemy_layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=approved,
            expected_revision=1,
        )
        is True
    )
    assert (await _stored(sqlalchemy_layer))["approval"]["status"] == "approved"

    stale = _record("approval-1", "rejected", 2)
    assert (
        await sqlalchemy_layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=stale,
            expected_revision=1,
        )
        is False
    )
    assert (await _stored(sqlalchemy_layer))["approval"]["status"] == "approved"


async def test_sqlalchemy_new_pending_replaces_a_terminal_record(
    sqlalchemy_layer: SQLAlchemyDataLayer,
):
    await sqlalchemy_layer.write_approval(
        thread_id="thread-1",
        step_id="step-1",
        record=_record("approval-1"),
        expected_revision=None,
    )
    await sqlalchemy_layer.write_approval(
        thread_id="thread-1",
        step_id="step-1",
        record=_record("approval-1", "approved", 2),
        expected_revision=1,
    )

    assert (
        await sqlalchemy_layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-2"),
            expected_revision=None,
        )
        is True
    )
    assert (await _stored(sqlalchemy_layer))["approval"]["id"] == "approval-2"

    assert (
        await sqlalchemy_layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-3"),
            expected_revision=None,
        )
        is False
    )


async def test_sqlalchemy_swallowed_error_is_not_success(
    sqlalchemy_layer: SQLAlchemyDataLayer, monkeypatch
):
    async def fail(*args, **kwargs):
        return None

    monkeypatch.setattr(sqlalchemy_layer, "execute_sql", fail)
    assert (
        await sqlalchemy_layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is False
    )


async def test_default_layer_detects_a_silent_drop():
    state: Dict[str, Any] = {"step": {"id": "step-1", "metadata": {}, "type": "tool"}}

    async def get_thread(thread_id):
        return {
            "id": thread_id,
            "createdAt": "2026-01-01T00:00:00Z",
            "name": None,
            "userId": None,
            "userIdentifier": "user-1",
            "metadata": {},
            "steps": [deepcopy(state["step"])],
            "elements": [],
            "tags": [],
        }

    async def drop(*args, **kwargs):
        return None

    layer = Mock(spec=BaseDataLayer)
    layer.get_thread = AsyncMock(side_effect=get_thread)
    layer.update_step = AsyncMock(side_effect=drop)
    typed = cast(BaseDataLayer, layer)

    assert (
        await BaseDataLayer.write_approval(
            typed,
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is False
    )


async def test_default_layer_read_back_matches_id_status_and_revision():
    state: Dict[str, Any] = {"step": {"id": "step-1", "metadata": {}, "type": "tool"}}

    async def get_thread(thread_id):
        return {
            "id": thread_id,
            "createdAt": "2026-01-01T00:00:00Z",
            "name": None,
            "userId": None,
            "userIdentifier": "user-1",
            "metadata": {},
            "steps": [deepcopy(state["step"])],
            "elements": [],
            "tags": [],
        }

    async def update_step(step):
        state["step"] = deepcopy(step)

    layer = Mock(spec=BaseDataLayer)
    layer.get_thread = AsyncMock(side_effect=get_thread)
    layer.update_step = AsyncMock(side_effect=update_step)
    typed = cast(BaseDataLayer, layer)

    assert (
        await BaseDataLayer.write_approval(
            typed,
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is True
    )
    assert state["step"]["metadata"]["approval"]["status"] == "pending"


def _dynamo_layer():
    client = Mock()
    return DynamoDBDataLayer(table_name="test-table", client=client), client


async def test_dynamodb_conditional_failure_is_not_success():
    layer, client = _dynamo_layer()
    client.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )

    assert (
        await layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is False
    )


async def test_dynamodb_conditional_write_uses_the_revision():
    layer, client = _dynamo_layer()

    assert (
        await layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1", "approved", 2),
            expected_revision=1,
        )
        is True
    )

    kwargs = client.update_item.call_args.kwargs
    assert "#ap.#revision = :expected" in kwargs["ConditionExpression"]
    assert kwargs["ExpressionAttributeValues"][":expected"] == {"N": "1"}
    assert kwargs["UpdateExpression"] == "SET #md.#ap = :record"


async def test_chainlit_data_layer_requires_returned_row():
    layer = ChainlitDataLayer(database_url="postgresql://unused")

    layer.execute_query = AsyncMock(return_value=[{"id": "step-1"}])
    assert (
        await layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is True
    )
    query = layer.execute_query.call_args.args[0]
    assert 'UPDATE "Step"' in query
    assert "RETURNING id" in query

    layer.execute_query = AsyncMock(return_value=[])
    assert (
        await layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is False
    )

    layer.execute_query = AsyncMock(side_effect=RuntimeError("db down"))
    assert (
        await layer.write_approval(
            thread_id="thread-1",
            step_id="step-1",
            record=_record("approval-1"),
            expected_revision=None,
        )
        is False
    )
