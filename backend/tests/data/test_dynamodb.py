"""Tests for the DynamoDB data layer's thread, step and deletion handling.

DynamoDB stores a thread as ``PK="THREAD#<thread_id>"`` and a step as
``SK="STEP#<step_id>"``. Reading those identifiers back must strip the prefix
only, never the characters the identifier happens to end with.
"""

import logging
import re
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from chainlit.data.dynamodb import DynamoDBDataLayer
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.types import FeedbackThreadLookup, Pagination, ThreadFilter, ThreadGrant


def _make_layer(items=None):
    client = Mock()
    client.query.return_value = {"Items": items or []}
    return DynamoDBDataLayer(table_name="test-table", client=client), client


@pytest.mark.asyncio
async def test_get_thread_owner_reads_the_typed_owner():
    layer, client = _make_layer()
    client.get_item.return_value = {
        "Item": {
            "userId": {"S": "user-1"},
            "userIdentifier": {"S": "alice@example.com"},
        }
    }

    owner = await layer.get_thread_owner("thread-1")

    assert owner is not None
    assert (owner.user_id, owner.identifier) == ("user-1", "alice@example.com")
    projection = client.get_item.call_args.kwargs["ProjectionExpression"]
    assert projection == "userId, userIdentifier"

    client.get_item.return_value = {}
    assert await layer.get_thread_owner("missing-thread") is None


@pytest.mark.asyncio
async def test_get_user_for_acl_does_not_log_the_identifier(caplog):
    layer, client = _make_layer()
    client.get_item.return_value = {
        "Item": {
            "PK": {"S": "USER#alice@example.com"},
            "SK": {"S": "USER"},
            "id": {"S": "user-1"},
            "identifier": {"S": "alice@example.com"},
            "createdAt": {"S": "2026-01-01T00:00:00Z"},
            "metadata": {"M": {}},
        }
    }

    # The child logger is pinned at WARNING; raise it so a logged identifier would be captured.
    caplog.set_level(logging.INFO, logger="chainlit.DynamoDB")

    with caplog.at_level(logging.INFO):
        user = await layer.get_user_for_acl("alice@example.com")

    assert logging.getLogger("chainlit.DynamoDB").isEnabledFor(logging.INFO)
    assert user is not None
    assert user.id == "user-1"
    assert "alice@example.com" not in caplog.text


@pytest.mark.asyncio
async def test_list_threads_keeps_thread_ids_ending_in_prefix_characters():
    """A thread id ending in T/H/R/E/A/D keeps every character."""
    layer, _ = _make_layer(
        [
            {
                "PK": {"S": "THREAD#DEMO"},
                "UserThreadSK": {"S": "TS#2026-01-01T00:00:00"},
                "name": {"S": "demo thread"},
            }
        ]
    )

    response = await layer.list_threads(
        Pagination(first=10), ThreadFilter(userId="user-1")
    )

    assert [thread["id"] for thread in response.data] == ["DEMO"]
    assert response.data[0]["createdAt"] == "2026-01-01T00:00:00"


@pytest.mark.asyncio
async def test_delete_feedback_builds_the_key_from_the_full_identifiers():
    """The delete key must reuse the ids from the feedback id verbatim."""
    layer, client = _make_layer()

    await layer.delete_feedback("THREAD#CODE::STEP#SEED")

    assert client.update_item.call_args.kwargs["Key"] == {
        "PK": {"S": "THREAD#CODE"},
        "SK": {"S": "STEP#SEED"},
    }


@pytest.mark.asyncio
async def test_delete_element_uses_the_explicit_thread(
    mock_chainlit_context, monkeypatch
):
    layer, client = _make_layer()
    fake_context = Mock()
    fake_context.session.thread_id = "context-thread"
    monkeypatch.setattr("chainlit.data.dynamodb.context", fake_context)

    async with mock_chainlit_context:
        await layer.delete_element("elem-1", "explicit-thread")

    assert client.delete_item.call_args.kwargs["Key"] == {
        "PK": {"S": "THREAD#explicit-thread"},
        "SK": {"S": "ELEMENT#elem-1"},
    }


class FakeDynamoDB:
    """Minimal DynamoDB stand-in with the UserThread GSI and 25-item batching."""

    def __init__(self, items, page_size=2, unprocessed_once=False, stale_gsi_items=()):
        self.items = [dict(item) for item in items]
        self.page_size = page_size
        self.unprocessed_once = unprocessed_once
        self.stale_gsi_items = list(stale_gsi_items)
        self.batch_sizes = []
        self.query_kwargs = []
        self.get_item_kwargs = []
        self.put_item_kwargs = []
        self.delete_item_kwargs = []
        self.update_kwargs = []
        self.scan_calls = []

    def _find(self, key):
        for item in self.items:
            if item["PK"] == key["PK"] and item["SK"] == key["SK"]:
                return item
        return None

    def _page(self, matched, exclusive_start_key):
        start = 0
        if exclusive_start_key:
            for index, item in enumerate(matched):
                if (
                    item["PK"] == exclusive_start_key["PK"]
                    and item["SK"] == exclusive_start_key["SK"]
                ):
                    start = index + 1
                    break
        page = matched[start : start + self.page_size]
        response = {"Items": page}
        if start + self.page_size < len(matched):
            response["LastEvaluatedKey"] = {"PK": page[-1]["PK"], "SK": page[-1]["SK"]}
        return response

    def query(self, **kwargs):
        self.query_kwargs.append(kwargs)
        pk = kwargs["ExpressionAttributeValues"][":pk"]["S"]
        if kwargs.get("IndexName") == "UserThread":
            matched = [
                item
                for item in [*self.items, *self.stale_gsi_items]
                if item.get("UserThreadPK", {}).get("S") == pk
            ]
        else:
            matched = [item for item in self.items if item["PK"]["S"] == pk]
            if "begins_with" in kwargs["KeyConditionExpression"]:
                prefix = kwargs["ExpressionAttributeValues"][":sk"]["S"]
                matched = [
                    item for item in matched if item["SK"]["S"].startswith(prefix)
                ]
        return self._page(matched, kwargs.get("ExclusiveStartKey"))

    def batch_write_item(self, RequestItems):
        table, requests = next(iter(RequestItems.items()))
        self.batch_sizes.append(len(requests))
        unprocessed = []
        if self.unprocessed_once and len(requests) > 1:
            self.unprocessed_once = False
            unprocessed, requests = requests[:1], requests[1:]
        for request in requests:
            key = request["DeleteRequest"]["Key"]
            self.items = [
                item
                for item in self.items
                if not (item["PK"] == key["PK"] and item["SK"] == key["SK"])
            ]
        return {"UnprocessedItems": {table: unprocessed}} if unprocessed else {}

    def delete_item(self, TableName, Key, ReturnValues=None):
        self.delete_item_kwargs.append({"Key": Key, "ReturnValues": ReturnValues})
        item = self._find(Key)
        if item is not None:
            self.items.remove(item)
        if ReturnValues == "ALL_OLD" and item is not None:
            return {"Attributes": item}
        return {}

    def get_item(self, **kwargs):
        self.get_item_kwargs.append(kwargs)
        item = self._find(kwargs["Key"])
        return {"Item": item} if item is not None else {}

    def put_item(self, TableName, Item, ConditionExpression=None):
        self.put_item_kwargs.append(
            {"Item": Item, "ConditionExpression": ConditionExpression}
        )
        existing = self._find(Item)
        if ConditionExpression == "attribute_not_exists(PK)" and existing is not None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "item exists",
                    }
                },
                "PutItem",
            )
        if existing is not None:
            self.items.remove(existing)
        self.items.append(dict(Item))

    def update_item(
        self,
        TableName,
        Key,
        UpdateExpression,
        ExpressionAttributeNames,
        ExpressionAttributeValues,
        **kwargs,
    ):
        self.update_kwargs.append({"Key": Key, "Expression": UpdateExpression})
        item = self._find(Key)
        if item is None:
            item = dict(Key)
            self.items.append(item)
        for name, value in re.findall(r"(#\w+) = (:\w+)", UpdateExpression):
            item[ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]
        return {}

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return {"Items": []}


def _thread_item(thread_id, user_id):
    return {
        "PK": {"S": f"THREAD#{thread_id}"},
        "SK": {"S": "THREAD"},
        "UserThreadPK": {"S": f"USER#{user_id}"},
        "UserThreadSK": {"S": "TS#2026-01-01T00:00:00"},
        "name": {"S": thread_id},
    }


def _stale_gsi_item(thread_id, user_id):
    """A UserThread row whose base-table partition is already gone."""
    return {
        "PK": {"S": f"THREAD#{thread_id}"},
        "SK": {"S": "THREAD"},
        "UserThreadPK": {"S": f"USER#{user_id}"},
        "UserThreadSK": {"S": "TS#2026-01-01T00:00:00"},
    }


def _step_item(thread_id, step_id):
    return {"PK": {"S": f"THREAD#{thread_id}"}, "SK": {"S": f"STEP#{step_id}"}}


def _element_item(thread_id, element_id, object_key):
    return {
        "PK": {"S": f"THREAD#{thread_id}"},
        "SK": {"S": f"ELEMENT#{element_id}"},
        "objectKey": {"S": object_key},
    }


def _seed_two_users():
    items = []
    for user_id, prefix in (("user-1", "target"), ("user-2", "other")):
        for index in range(2):
            thread_id = f"{prefix}-thread-{index}"
            items.append(_thread_item(thread_id, user_id))
            items.append(_step_item(thread_id, f"{prefix}-step-{index}"))
            items.append(
                _element_item(
                    thread_id, f"{prefix}-element-{index}", f"{prefix}-object-{index}"
                )
            )
    return items


def _make_storage():
    storage = AsyncMock(spec=BaseStorageClient)
    storage.delete_file.return_value = True
    return storage


def _make_delete_layer(
    items, page_size=2, unprocessed_once=False, storage=None, stale_gsi_items=()
):
    client = FakeDynamoDB(
        items,
        page_size=page_size,
        unprocessed_once=unprocessed_once,
        stale_gsi_items=stale_gsi_items,
    )
    layer = DynamoDBDataLayer(
        table_name="test-table",
        client=client,
        storage_provider=storage if storage is not None else _make_storage(),
    )
    return layer, client


@pytest.mark.asyncio
async def test_delete_all_threads_removes_only_the_target_users_items():
    storage = _make_storage()
    layer, client = _make_delete_layer(_seed_two_users(), storage=storage)

    assert await layer.delete_all_threads("user-1") == 2

    remaining_partitions = {item["PK"]["S"] for item in client.items}
    assert remaining_partitions == {
        "THREAD#other-thread-0",
        "THREAD#other-thread-1",
    }
    delete_file = cast(AsyncMock, storage.delete_file)
    assert {call.kwargs["object_key"] for call in delete_file.await_args_list} == {
        "target-object-0",
        "target-object-1",
    }

    assert await layer.delete_all_threads("user-1") == 0
    assert delete_file.await_count == 2


@pytest.mark.asyncio
async def test_delete_all_threads_ignores_a_stale_gsi_entry():
    storage = _make_storage()
    layer, client = _make_delete_layer(
        [
            _thread_item("live-thread", "user-1"),
            _element_item("live-thread", "live-element", "live-object"),
        ],
        storage=storage,
        stale_gsi_items=[_stale_gsi_item("already-deleted-thread", "user-1")],
    )

    assert await layer.delete_all_threads("user-1") == 1

    assert client.items == []
    delete_file = cast(AsyncMock, storage.delete_file)
    assert [call.kwargs["object_key"] for call in delete_file.await_args_list] == [
        "live-object"
    ]
    assert await layer.delete_all_threads("user-1") == 0


@pytest.mark.asyncio
async def test_delete_all_threads_paginates_past_one_gsi_page():
    items = []
    for index in range(5):
        thread_id = f"thread-{index}"
        items.append(_thread_item(thread_id, "user-1"))
        items.append(_step_item(thread_id, f"step-{index}"))

    layer, client = _make_delete_layer(items, page_size=2)

    assert await layer.delete_all_threads("user-1") == 5
    assert client.items == []


@pytest.mark.asyncio
async def test_delete_all_threads_batches_at_25_and_retries_unprocessed(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("chainlit.data.dynamodb.asyncio.sleep", no_sleep)

    items = [_thread_item("big-thread", "user-1")]
    items += [_step_item("big-thread", f"step-{index}") for index in range(30)]
    layer, client = _make_delete_layer(items, page_size=100, unprocessed_once=True)

    assert await layer.delete_all_threads("user-1") == 1

    assert client.items == []
    assert max(client.batch_sizes) == 25
    assert len(client.batch_sizes) > 2


@pytest.mark.asyncio
async def test_delete_thread_retries_after_a_partial_storage_failure():
    storage = _make_storage()
    storage.delete_file.side_effect = [True, False, True, True]
    items = [
        _thread_item("target-thread", "user-1"),
        _element_item("target-thread", "element-a", "object-a"),
        _element_item("target-thread", "element-b", "object-b"),
    ]
    layer, client = _make_delete_layer(items, storage=storage)

    with pytest.raises(RuntimeError, match="storage object"):
        await layer.delete_thread("target-thread")
    assert client.items != []

    await layer.delete_thread("target-thread")

    assert client.items == []
    assert cast(AsyncMock, storage.delete_file).await_count == 4


@pytest.mark.asyncio
async def test_delete_all_threads_retries_after_a_partial_storage_failure():
    storage = _make_storage()
    storage.delete_file.side_effect = [True, False, True, True]
    items = [
        _thread_item("thread-a", "user-1"),
        _element_item("thread-a", "element-a", "object-a"),
        _thread_item("thread-b", "user-1"),
        _element_item("thread-b", "element-b", "object-b"),
    ]
    layer, client = _make_delete_layer(items, storage=storage)

    with pytest.raises(RuntimeError, match="storage object"):
        await layer.delete_all_threads("user-1")
    assert client.items != []

    assert await layer.delete_all_threads("user-1") == 2
    assert client.items == []
    assert cast(AsyncMock, storage.delete_file).await_count == 4


@pytest.mark.asyncio
async def test_delete_thread_queries_the_partition_with_consistent_read():
    layer, client = _make_delete_layer(
        [
            _thread_item("target-thread", "user-1"),
            _step_item("target-thread", "step-0"),
        ]
    )

    await layer.delete_thread("target-thread")

    assert client.query_kwargs
    assert all(kwargs.get("ConsistentRead") is True for kwargs in client.query_kwargs)


@pytest.mark.asyncio
async def test_delete_thread_deletes_storage_objects():
    storage = _make_storage()
    layer, client = _make_delete_layer(_seed_two_users(), storage=storage)

    await layer.delete_thread("target-thread-0")

    delete_file = cast(AsyncMock, storage.delete_file)
    assert [call.kwargs["object_key"] for call in delete_file.await_args_list] == [
        "target-object-0"
    ]
    remaining_partitions = {item["PK"]["S"] for item in client.items}
    assert "THREAD#target-thread-0" not in remaining_partitions
    assert "THREAD#target-thread-1" in remaining_partitions
    assert "THREAD#other-thread-0" in remaining_partitions


@pytest.mark.asyncio
async def test_delete_thread_deletes_an_element_written_while_deleting():
    """A blob uploaded mid-erasure still has its row when the loop sees it."""
    storage = _make_storage()
    layer, client = _make_delete_layer(
        [
            _thread_item("target-thread", "user-1"),
            _element_item("target-thread", "element-a", "object-a"),
        ],
        storage=storage,
    )
    late_element = _element_item("target-thread", "element-b", "object-b")

    async def delete_blob(object_key):
        if object_key == "object-a" and late_element not in client.items:
            client.items.append(late_element)
        return True

    storage.delete_file.side_effect = delete_blob

    await layer.delete_thread("target-thread")

    delete_file = cast(AsyncMock, storage.delete_file)
    assert {call.kwargs["object_key"] for call in delete_file.await_args_list} == {
        "object-a",
        "object-b",
    }
    assert client.items == []


@pytest.mark.asyncio
async def test_delete_all_threads_keeps_items_when_a_storage_delete_fails():
    storage = _make_storage()
    storage.delete_file.return_value = False
    layer, client = _make_delete_layer(_seed_two_users(), storage=storage)
    original_items = list(client.items)

    with pytest.raises(RuntimeError, match="storage object"):
        await layer.delete_all_threads("user-1")

    assert client.items == original_items


@pytest.mark.asyncio
async def test_delete_thread_keeps_items_when_a_storage_delete_fails():
    storage = _make_storage()
    storage.delete_file.return_value = False
    layer, client = _make_delete_layer(_seed_two_users(), storage=storage)
    original_items = list(client.items)

    with pytest.raises(RuntimeError, match="storage object"):
        await layer.delete_thread("target-thread-0")

    assert client.items == original_items


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


def _grant_item(
    thread_id: str = "thread-1",
    grantee_user_id: str = "bob-id",
    grantee_user_created_at: str = "2026-01-01T00:00:00Z",
    role: str = "read",
) -> dict:
    return {
        "PK": {"S": f"THREAD#{thread_id}"},
        "SK": {"S": f"GRANT#{grantee_user_id}"},
        "granteeUserCreatedAt": {"S": grantee_user_created_at},
        "granteeIdentifier": {"S": "bob"},
        "grantedByUserId": {"S": "alice-id"},
        "grantedAt": {"S": "2026-01-02T00:00:00Z"},
        "role": {"S": role},
    }


def test_thread_grants_are_a_layer_capability():
    layer, _ = _make_layer()

    assert layer.thread_grants_supported is True
    assert layer.feedback_authorization_supported is True


@pytest.mark.asyncio
async def test_thread_grant_crud():
    layer, client = _make_delete_layer([_thread_item("thread-1", "user-1")])

    await layer.create_thread_grant(_grant())

    assert await layer.get_thread_grant("thread-1", "bob-id") == _grant()
    assert await layer.list_thread_grants("thread-1") == [_grant()]

    assert await layer.delete_thread_grant("thread-1", "bob-id") is True
    assert await layer.get_thread_grant("thread-1", "bob-id") is None
    assert await layer.list_thread_grants("thread-1") == []
    assert await layer.delete_thread_grant("thread-1", "bob-id") is False
    assert client.scan_calls == []


@pytest.mark.asyncio
async def test_duplicate_thread_grant_keeps_the_first_generation():
    layer, _ = _make_delete_layer([])

    await layer.create_thread_grant(_grant())
    await layer.create_thread_grant(
        _grant(grantee_user_created_at="2027-01-01T00:00:00Z")
    )

    stored = await layer.get_thread_grant("thread-1", "bob-id")
    assert stored is not None
    assert stored.grantee_user_created_at == "2026-01-01T00:00:00Z"
    assert len(await layer.list_thread_grants("thread-1")) == 1


@pytest.mark.asyncio
async def test_thread_grant_lookup_is_scoped():
    layer, _ = _make_delete_layer([])
    await layer.create_thread_grant(_grant())

    assert await layer.get_thread_grant("thread-1", "carol-id") is None
    assert await layer.get_thread_grant("thread-2", "bob-id") is None


@pytest.mark.asyncio
async def test_thread_grant_lookup_denies_a_non_read_role():
    layer, _ = _make_delete_layer([])
    await layer.create_thread_grant(_grant(role="write"))

    assert await layer.get_thread_grant("thread-1", "bob-id") is None


@pytest.mark.asyncio
async def test_thread_grant_lookup_denies_a_missing_generation():
    layer, client = _make_delete_layer([])
    item = _grant_item()
    del item["granteeUserCreatedAt"]
    client.items.append(item)

    assert await layer.get_thread_grant("thread-1", "bob-id") is None


@pytest.mark.asyncio
async def test_thread_grant_item_stays_in_the_thread_partition():
    layer, client = _make_delete_layer([])

    await layer.create_thread_grant(_grant())

    item = client.items[0]
    assert item["PK"] == {"S": "THREAD#thread-1"}
    assert item["SK"] == {"S": "GRANT#bob-id"}
    assert item["granteeUserCreatedAt"] == {"S": "2026-01-01T00:00:00Z"}
    assert item["role"] == {"S": "read"}
    assert "UserThreadPK" not in item
    assert "UserThreadSK" not in item


@pytest.mark.asyncio
async def test_thread_grant_lookup_is_one_consistent_get_item():
    layer, client = _make_layer()
    client.get_item.return_value = {"Item": _grant_item()}

    assert await layer.get_thread_grant("thread-1", "bob-id") == _grant()

    assert client.get_item.call_count == 1
    assert client.get_item.call_args.kwargs["Key"] == {
        "PK": {"S": "THREAD#thread-1"},
        "SK": {"S": "GRANT#bob-id"},
    }
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True
    assert client.delete_item.call_count == 0
    assert client.scan.call_count == 0


@pytest.mark.asyncio
async def test_revoke_is_one_delete_item():
    layer, client = _make_layer()
    client.delete_item.return_value = {"Attributes": _grant_item()}

    assert await layer.delete_thread_grant("thread-1", "bob-id") is True

    assert client.delete_item.call_count == 1
    assert client.delete_item.call_args.kwargs["ReturnValues"] == "ALL_OLD"


@pytest.mark.asyncio
async def test_delete_thread_removes_its_grants_and_is_idempotent():
    layer, client = _make_delete_layer(
        [
            _thread_item("thread-1", "user-1"),
            _step_item("thread-1", "step-1"),
        ]
    )
    await layer.create_thread_grant(_grant())

    await layer.delete_thread("thread-1")

    assert await layer.list_thread_grants("thread-1") == []
    assert client.items == []

    await layer.delete_thread("thread-1")
    assert client.items == []


@pytest.mark.asyncio
async def test_delete_all_threads_removes_only_the_owners_grants():
    layer, _ = _make_delete_layer(
        [
            _thread_item("alice-thread", "alice-id"),
            _thread_item("bob-thread", "bob-id"),
        ]
    )
    await layer.create_thread_grant(_grant(thread_id="alice-thread"))
    await layer.create_thread_grant(_grant(thread_id="bob-thread"))

    assert await layer.delete_all_threads("alice-id") == 1

    assert await layer.list_thread_grants("alice-thread") == []
    assert await layer.list_thread_grants("bob-thread") == [
        _grant(thread_id="bob-thread")
    ]


@pytest.mark.asyncio
async def test_grantee_clear_all_leaves_the_owners_thread_and_grant():
    layer, client = _make_delete_layer([_thread_item("alice-thread", "alice-id")])
    await layer.create_thread_grant(
        _grant(thread_id="alice-thread", grantee_user_id="bob-id")
    )

    assert await layer.delete_all_threads("bob-id") == 0

    assert any(item["SK"] == {"S": "THREAD"} for item in client.items)
    assert await layer.list_thread_grants("alice-thread") == [
        _grant(thread_id="alice-thread", grantee_user_id="bob-id")
    ]


@pytest.mark.asyncio
async def test_update_thread_without_an_owner_keeps_grants():
    layer, _ = _make_delete_layer([_thread_item("thread-1", "user-1")])
    await layer.update_thread("thread-1", user_id="alice-id")
    await layer.create_thread_grant(_grant())

    await layer.update_thread("thread-1", name="renamed")

    assert await layer.list_thread_grants("thread-1") == [_grant()]


@pytest.mark.asyncio
async def test_update_thread_owner_change_removes_grants():
    layer, _ = _make_delete_layer([_thread_item("thread-1", "user-1")])
    await layer.update_thread("thread-1", user_id="alice-id")
    await layer.create_thread_grant(_grant())

    await layer.update_thread("thread-1", user_id="alice-id")
    assert await layer.list_thread_grants("thread-1") == [_grant()]

    await layer.update_thread("thread-1", user_id="bob-id")
    assert await layer.list_thread_grants("thread-1") == []


@pytest.mark.asyncio
async def test_get_feedback_thread_id_parses_the_key_without_a_read():
    layer, client = _make_layer()

    lookup = await layer.get_feedback_thread_id("THREAD#thread-1::STEP#step-1")

    assert lookup == FeedbackThreadLookup(status="found", thread_id="thread-1")
    assert client.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "feedback_id",
    [
        "missing-feedback",
        "THREAD#::STEP#step-1",
        "THREAD#thread-1",
        "thread-1::STEP#step-1",
        "THREAD#thread-1::step-1",
    ],
)
async def test_get_feedback_thread_id_parse_failure_is_not_found(feedback_id):
    layer, _ = _make_layer()

    assert await layer.get_feedback_thread_id(feedback_id) == FeedbackThreadLookup(
        status="not_found"
    )


@pytest.mark.asyncio
async def test_step_belongs_to_thread_is_one_consistent_get_item():
    layer, client = _make_layer()
    client.get_item.return_value = {"Item": {"PK": {"S": "THREAD#thread-1"}}}

    assert await layer.step_belongs_to_thread("thread-1", "step-1") is True

    assert client.get_item.call_count == 1
    assert client.get_item.call_args.kwargs["Key"] == {
        "PK": {"S": "THREAD#thread-1"},
        "SK": {"S": "STEP#step-1"},
    }
    assert client.get_item.call_args.kwargs["ConsistentRead"] is True

    client.get_item.return_value = {}
    assert await layer.step_belongs_to_thread("thread-1", "step-2") is False


@pytest.mark.asyncio
async def test_step_belongs_to_thread_denies_on_error():
    layer, client = _make_layer()
    client.get_item.side_effect = RuntimeError("boom")

    assert await layer.step_belongs_to_thread("thread-1", "step-1") is False


@pytest.mark.asyncio
async def test_list_and_get_thread_omit_a_stale_shared_item():
    step = _step_item("thread-1", "step-1")
    step["id"] = {"S": "step-1"}
    step["createdAt"] = {"S": "2026-01-01T00:00:00"}
    stale_shared = {
        "PK": {"S": "THREAD#thread-1"},
        "SK": {"S": "SHARED#bob-id"},
        "sharedWithUserId": {"S": "bob-id"},
    }
    layer, _ = _make_delete_layer(
        [_thread_item("thread-1", "user-1"), step, stale_shared], page_size=10
    )

    listed = await layer.list_threads(
        Pagination(first=10), ThreadFilter(userId="user-1")
    )
    assert [thread["id"] for thread in listed.data] == ["thread-1"]

    thread = await layer.get_thread("thread-1")
    assert thread is not None
    assert "sharedWithUserId" not in thread
    assert [s["id"] for s in thread["steps"]] == ["step-1"]
