import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest

from chainlit.data.chainlit_data_layer import (
    ChainlitDataLayer,
    _datetime_to_utc_iso,
    _parse_iso_datetime,
)
from chainlit.data.storage_clients.base import BaseStorageClient
from chainlit.element import Element, File, Image, Pdf


@pytest.mark.asyncio
async def test_update_thread_preserves_metadata_when_none_existing_thread():
    """Test that update_thread preserves existing metadata when metadata=None on an existing thread."""
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test", storage_client=None, show_logger=False
    )

    existing_metadata = {"important": "data", "keep": "me"}

    async def mock_execute_query(query, params):
        if "SELECT" in query and "metadata" in query:
            return [{"metadata": json.dumps(existing_metadata)}]
        return None

    data_layer.execute_query = AsyncMock(side_effect=mock_execute_query)

    await data_layer.update_thread(thread_id="test-thread-123", name="Updated Name")

    assert data_layer.execute_query.call_count == 2

    update_call = data_layer.execute_query.call_args_list[1]
    update_params = update_call[0][1]

    metadata_json = None
    for value in update_params.values():
        if isinstance(value, str) and value.startswith("{"):
            try:
                metadata_json = json.loads(value)
                break
            except json.JSONDecodeError:
                pass

    assert metadata_json == existing_metadata


@pytest.mark.asyncio
async def test_update_thread_noop_skips_upsert_on_existing_thread():
    """Test that update_thread with no arguments early-returns for an existing thread."""
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test", storage_client=None, show_logger=False
    )

    async def mock_execute_query(query, params):
        if "SELECT" in query and "metadata" in query:
            return [{"metadata": json.dumps({"existing": "data"})}]
        return None

    data_layer.execute_query = AsyncMock(side_effect=mock_execute_query)

    await data_layer.update_thread(thread_id="test-thread-123")

    assert data_layer.execute_query.call_count == 1


@pytest.mark.asyncio
async def test_update_thread_new_thread_includes_empty_metadata():
    """Test that update_thread includes metadata='{}' for a new thread (NOT NULL safe)."""
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test", storage_client=None, show_logger=False
    )

    async def mock_execute_query(query, params):
        if "SELECT" in query and "metadata" in query:
            return []
        return None

    data_layer.execute_query = AsyncMock(side_effect=mock_execute_query)

    await data_layer.update_thread(thread_id="new-thread", name="New Chat")

    assert data_layer.execute_query.call_count == 2

    update_call = data_layer.execute_query.call_args_list[1]
    update_query = update_call[0][0]
    update_params = update_call[0][1]

    assert "metadata" in update_query.lower()

    metadata_json = None
    for value in update_params.values():
        if isinstance(value, str) and value.startswith("{"):
            try:
                metadata_json = json.loads(value)
                break
            except json.JSONDecodeError:
                pass

    assert metadata_json == {}


@pytest.mark.asyncio
async def test_update_thread_merges_metadata_when_provided():
    """Test that update_thread merges metadata correctly when provided."""
    # Create a mock data layer
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test", storage_client=None, show_logger=False
    )

    # Mock the execute_query method to return existing metadata
    existing_metadata = {"is_shared": True, "custom_field": "original"}

    async def mock_execute_query(query, params):
        if "SELECT" in query and "metadata" in query:
            # Return existing thread metadata
            return [{"metadata": json.dumps(existing_metadata)}]
        # For the UPDATE/INSERT, return None
        return None

    data_layer.execute_query = AsyncMock(side_effect=mock_execute_query)

    # Call update_thread with partial metadata update
    new_metadata = {"custom_field": "updated", "new_field": "added"}
    await data_layer.update_thread(
        thread_id="test-thread-123", name="Updated Name", metadata=new_metadata
    )

    # Verify execute_query was called twice (once for SELECT, once for UPDATE)
    assert data_layer.execute_query.call_count == 2

    # Get the UPDATE call
    update_call = data_layer.execute_query.call_args_list[1]
    update_params = update_call[0][1]

    # The metadata should be merged
    # Expected: {"is_shared": True, "custom_field": "updated", "new_field": "added"}
    # Find the JSON metadata in the params
    metadata_json = None
    for value in update_params.values():
        if isinstance(value, str) and value.startswith("{"):
            try:
                metadata_json = json.loads(value)
                break
            except json.JSONDecodeError:
                pass

    assert metadata_json is not None
    assert metadata_json.get("is_shared") is True
    assert metadata_json.get("custom_field") == "updated"
    assert metadata_json.get("new_field") == "added"


@pytest.mark.asyncio
async def test_update_thread_deletes_keys_with_none_values():
    """Test that update_thread deletes keys when value is None."""
    # Create a mock data layer
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test", storage_client=None, show_logger=False
    )

    # Mock the execute_query method to return existing metadata
    existing_metadata = {
        "is_shared": True,
        "to_delete": "will be removed",
        "keep": "stays",
    }

    async def mock_execute_query(query, params):
        if "SELECT" in query and "metadata" in query:
            # Return existing thread metadata
            return [{"metadata": json.dumps(existing_metadata)}]
        # For the UPDATE/INSERT, return None
        return None

    data_layer.execute_query = AsyncMock(side_effect=mock_execute_query)

    # Call update_thread with None value to delete a key
    new_metadata = {"to_delete": None, "new_field": "added"}
    await data_layer.update_thread(thread_id="test-thread-123", metadata=new_metadata)

    # Verify execute_query was called twice
    assert data_layer.execute_query.call_count == 2

    # Get the UPDATE call
    update_call = data_layer.execute_query.call_args_list[1]
    update_params = update_call[0][1]

    # The metadata should have deleted "to_delete" key and added "new_field"
    # Expected: {"is_shared": True, "keep": "stays", "new_field": "added"}
    metadata_json = None
    for value in update_params.values():
        if isinstance(value, str) and value.startswith("{"):
            try:
                metadata_json = json.loads(value)
                break
            except json.JSONDecodeError:
                pass

    if metadata_json:
        # Verify "to_delete" is not in the merged metadata
        assert "to_delete" not in metadata_json
        # Verify "new_field" was added
        assert metadata_json.get("new_field") == "added"
        # Verify "is_shared" and "keep" are preserved
        assert metadata_json.get("is_shared") is True
        assert metadata_json.get("keep") == "stays"


@pytest.mark.asyncio
async def test_create_step_uses_nullif_for_output_and_input():
    """Empty-string output/input should not overwrite existing content.

    Regression test for https://github.com/Chainlit/chainlit/issues/2789
    The SQL uses NULLIF(EXCLUDED.output, '') so that an empty string from the
    initial Step.send() is treated as NULL by COALESCE, preventing it from
    overwriting non-empty content saved by a subsequent Step.update().
    """
    import inspect

    source = inspect.getsource(ChainlitDataLayer.create_step)

    assert "NULLIF(EXCLUDED.output, '')" in source, (
        "output should use NULLIF to treat empty string as NULL"
    )
    assert "NULLIF(EXCLUDED.input, '')" in source, (
        "input should use NULLIF to treat empty string as NULL"
    )


class TestIsoTimestampHelpers:
    def test_parse_with_and_without_z(self):
        with_z = _parse_iso_datetime("2025-09-04T02:00:42.164000Z")
        without_z = _parse_iso_datetime("2025-09-04T02:00:42.164000")
        assert with_z == without_z == datetime(2025, 9, 4, 2, 0, 42, 164000)

    def test_parse_rejects_other_formats(self):
        with pytest.raises(ValueError, match="does not match format"):
            _parse_iso_datetime("2025-09-04 02:00:42")

    def test_roundtrip_adds_z(self):
        original = "2025-09-04T02:00:42.164000"
        parsed = _parse_iso_datetime(original)
        assert _datetime_to_utc_iso(parsed) == original + "Z"

    def test_handles_aware_datetimes(self):
        dt = datetime.fromisoformat("2025-09-04T04:00:42.164000+02:00")
        assert _datetime_to_utc_iso(dt) == "2025-09-04T02:00:42.164000Z"

    def test_step_row_timestamps_are_reparseable(self):
        layer = ChainlitDataLayer(database_url="postgresql://fake", storage_client=None)
        row = {
            "id": "step-1",
            "threadId": "thread-1",
            "parentId": None,
            "name": "test_step",
            "type": "run",
            "input": "{}",
            "output": "{}",
            "metadata": "{}",
            "createdAt": datetime(2025, 9, 4, 2, 0, 42, 164000),
            "startTime": datetime(2025, 9, 4, 2, 0, 42, 164000),
            "endTime": datetime(2025, 9, 4, 2, 0, 43),
            "showInput": "json",
            "isError": False,
            "feedback_id": None,
        }

        result = layer._convert_step_row_to_dict(row)

        assert result["createdAt"] == "2025-09-04T02:00:42.164000Z"
        assert result["start"] == "2025-09-04T02:00:42.164000Z"
        assert result["end"] == "2025-09-04T02:00:43Z"
        assert _parse_iso_datetime(result["createdAt"]) == row["createdAt"]

    def test_step_row_none_timestamps_stay_none(self):
        layer = ChainlitDataLayer(database_url="postgresql://fake", storage_client=None)
        row = {
            "id": "step-1",
            "threadId": "thread-1",
            "parentId": None,
            "name": "test_step",
            "type": "run",
            "input": "{}",
            "output": "{}",
            "metadata": "{}",
            "createdAt": None,
            "startTime": None,
            "endTime": None,
            "showInput": "json",
            "isError": False,
            "feedback_id": None,
        }

        result = layer._convert_step_row_to_dict(row)

        assert result["createdAt"] is None
        assert result["start"] is None
        assert result["end"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("element_type", [Image, Pdf])
async def test_create_element_uses_inline_disposition_for_browser_rendered_types(
    monkeypatch: pytest.MonkeyPatch,
    mock_chainlit_context,
    mock_storage_client: BaseStorageClient,
    element_type: type[Element],
):
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test",
        storage_client=mock_storage_client,
        show_logger=False,
    )
    monkeypatch.setattr(
        data_layer, "execute_query", AsyncMock(return_value=[{"id": "existing"}])
    )

    async with mock_chainlit_context:
        element = element_type(
            name="rendered-element",
            content=b"content",
            for_id="test-step",
        )
        await data_layer.create_element(element)

    upload_file = cast(AsyncMock, mock_storage_client.upload_file)
    upload_file.assert_awaited_once()
    upload_call = upload_file.await_args
    assert upload_call is not None
    assert upload_call.kwargs["content_disposition"] == "inline"


@pytest.mark.asyncio
async def test_create_element_preserves_attachment_disposition_for_generic_files(
    monkeypatch: pytest.MonkeyPatch,
    mock_chainlit_context,
    mock_storage_client: BaseStorageClient,
):
    data_layer = ChainlitDataLayer(
        database_url="postgresql://test",
        storage_client=mock_storage_client,
        show_logger=False,
    )
    monkeypatch.setattr(
        data_layer, "execute_query", AsyncMock(return_value=[{"id": "existing"}])
    )

    async with mock_chainlit_context:
        element = File(
            name="report.txt",
            content=b"content",
            for_id="test-step",
        )
        await data_layer.create_element(element)

    upload_file = cast(AsyncMock, mock_storage_client.upload_file)
    upload_file.assert_awaited_once()
    upload_call = upload_file.await_args
    assert upload_call is not None
    assert upload_call.kwargs["content_disposition"] == (
        'attachment; filename="report.txt"'
    )


@pytest.mark.asyncio
async def test_get_element_preserves_none_fields():
    layer = ChainlitDataLayer(database_url="postgresql://fake", storage_client=None)
    layer.execute_query = AsyncMock(
        return_value=[
            {
                "id": "elem-1",
                "threadId": "thread-1",
                "stepId": "step-1",
                "metadata": json.dumps({"type": "file"}),
                "url": None,
                "name": "test.txt",
                "mime": None,
                "objectKey": None,
                "chainlitKey": "ck-1",
                "display": "inline",
                "size": None,
                "language": None,
                "page": None,
                "autoPlay": None,
                "playerConfig": None,
                "props": "{}",
            }
        ]
    )

    result = await layer.get_element("thread-1", "elem-1")

    assert result is not None
    assert result["url"] is None
    assert result["objectKey"] is None
    assert result["mime"] is None


class _SqliteFetchConnection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    async def fetch(self, query: str, *params):
        cursor = self._conn.execute(re.sub(r"\$(\d+)", "?", query), params)
        if cursor.description is None:
            return []
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


class _SqlitePool:
    def __init__(self, conn: sqlite3.Connection):
        self._connection = _SqliteFetchConnection(conn)

    @asynccontextmanager
    async def acquire(self):
        yield self._connection


@pytest.fixture
def sqlite_chainlit_layer(
    monkeypatch: pytest.MonkeyPatch, mock_storage_client: BaseStorageClient
):
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE "User" (
            id TEXT PRIMARY KEY,
            identifier TEXT NOT NULL UNIQUE
        );
        CREATE TABLE "Thread" (
            id TEXT PRIMARY KEY,
            "userId" TEXT REFERENCES "User"(id) ON DELETE CASCADE
        );
        CREATE TABLE "Step" (
            id TEXT PRIMARY KEY,
            "threadId" TEXT REFERENCES "Thread"(id) ON DELETE CASCADE
        );
        CREATE TABLE "Element" (
            id TEXT PRIMARY KEY,
            "threadId" TEXT REFERENCES "Thread"(id) ON DELETE CASCADE,
            "stepId" TEXT REFERENCES "Step"(id) ON DELETE CASCADE,
            "objectKey" TEXT
        );
        CREATE TABLE "Feedback" (
            id TEXT PRIMARY KEY,
            "stepId" TEXT REFERENCES "Step"(id) ON DELETE CASCADE
        );
        """
    )
    layer = ChainlitDataLayer(
        database_url="postgresql://test", storage_client=mock_storage_client
    )
    monkeypatch.setattr(layer, "pool", _SqlitePool(conn))
    yield layer, conn
    conn.close()


@pytest.mark.asyncio
async def test_delete_all_threads_only_deletes_target_user(
    sqlite_chainlit_layer,
    mock_storage_client: BaseStorageClient,
):
    layer, conn = sqlite_chainlit_layer
    conn.executemany(
        'INSERT INTO "User" (id, identifier) VALUES (?, ?)',
        [("target-user", "target"), ("other-user", "other")],
    )
    conn.executemany(
        'INSERT INTO "Thread" (id, "userId") VALUES (?, ?)',
        [
            ("target-thread-0", "target-user"),
            ("target-thread-1", "target-user"),
            ("other-thread-0", "other-user"),
            ("other-thread-1", "other-user"),
        ],
    )
    conn.executemany(
        'INSERT INTO "Step" (id, "threadId") VALUES (?, ?)',
        [
            ("target-step-0", "target-thread-0"),
            ("target-step-1", "target-thread-1"),
            ("other-step-0", "other-thread-0"),
            ("other-step-1", "other-thread-1"),
        ],
    )
    conn.executemany(
        'INSERT INTO "Element" (id, "threadId", "stepId", "objectKey") '
        "VALUES (?, ?, ?, ?)",
        [
            ("target-element-0", "target-thread-0", "target-step-0", "target-object-0"),
            ("target-element-1", "target-thread-1", "target-step-1", "target-object-1"),
            ("other-element-0", "other-thread-0", "other-step-0", "other-object-0"),
            ("other-element-1", "other-thread-1", "other-step-1", "other-object-1"),
        ],
    )
    conn.executemany(
        'INSERT INTO "Feedback" (id, "stepId") VALUES (?, ?)',
        [
            ("target-feedback-0", "target-step-0"),
            ("target-feedback-1", "target-step-1"),
            ("other-feedback-0", "other-step-0"),
            ("other-feedback-1", "other-step-1"),
        ],
    )
    conn.commit()

    deleted = await layer.delete_all_threads("target-user")

    delete_file = cast(AsyncMock, mock_storage_client.delete_file)
    assert deleted == 2
    assert {call.kwargs["object_key"] for call in delete_file.await_args_list} == {
        "target-object-0",
        "target-object-1",
    }

    assert await layer.delete_all_threads("target-user") == 0
    assert delete_file.await_count == 2

    assert (
        conn.execute(
            'SELECT COUNT(*) FROM "Thread" WHERE "userId" = ?', ("target-user",)
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            'SELECT COUNT(*) FROM "Thread" WHERE "userId" = ?', ("other-user",)
        ).fetchone()[0]
        == 2
    )

    for table, column in (
        ("Step", "threadId"),
        ("Element", "threadId"),
        ("Feedback", "stepId"),
    ):
        target_count = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" LIKE ?', ("target-%",)
        ).fetchone()[0]
        other_count = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" LIKE ?', ("other-%",)
        ).fetchone()[0]
        assert target_count == 0
        assert other_count == 2
