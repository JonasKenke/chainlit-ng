"""SQLAlchemy scheduled-task persistence and lease tests."""

from pathlib import Path

import pytest

from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.types import ScheduledTaskCompletion, ScheduledTaskDict


@pytest.fixture
async def data_layer(tmp_path: Path):
    layer = SQLAlchemyDataLayer(
        f"sqlite+aiosqlite:///{tmp_path / 'scheduled.sqlite'}",
        storage_provider=None,
    )
    yield layer
    await layer.close()


@pytest.fixture
def task() -> ScheduledTaskDict:
    return ScheduledTaskDict(
        id="task-1",
        userId="user-1",
        userIdentifier="one@example.com",
        prompt="hello",
        chatProfile=None,
        model="🔷 Gemini 3.5 Flash",
        cron="* * * * *",
        timezone="UTC",
        enabled=True,
        afterHooks=["Email"],
        nextRunAt="2000-01-01T00:00:00Z",
        manualRunRequestedAt=None,
        lastRunAt=None,
        lastStatus=None,
        lastError=None,
        lastThreadId=None,
        leaseId=None,
        leaseUntil=None,
        createdAt="2000-01-01T00:00:00Z",
        updatedAt="2000-01-01T00:00:00Z",
    )


async def test_crud_and_owner_scope(data_layer, task):
    await data_layer.create_scheduled_task(task)
    stored = await data_layer.get_scheduled_task(task["id"])
    assert stored["afterHooks"] == ["Email"]
    assert stored["model"] == "🔷 Gemini 3.5 Flash"
    assert len(await data_layer.list_scheduled_tasks("user-1")) == 1
    assert await data_layer.list_scheduled_tasks("other") == []
    assert await data_layer.delete_scheduled_task(task["id"], "other") is False
    assert await data_layer.delete_scheduled_task(task["id"], "user-1") is True


async def test_manual_run_request_is_owner_scoped_and_compare_and_set(data_layer, task):
    await data_layer.create_scheduled_task(task)
    assert (
        await data_layer.request_scheduled_task_run(
            task["id"], "other", "2020-01-01T00:00:00Z"
        )
        is False
    )
    assert (
        await data_layer.request_scheduled_task_run(
            task["id"], "user-1", "2020-01-01T00:00:00Z"
        )
        is True
    )
    assert (
        await data_layer.request_scheduled_task_run(
            task["id"], "user-1", "2020-01-01T00:01:00Z"
        )
        is False
    )


async def test_claim_renew_and_stale_completion(data_layer, task):
    await data_layer.create_scheduled_task(task)
    first = await data_layer.claim_due_scheduled_tasks(
        "2020-01-01T00:00:00Z", "2020-01-01T00:01:00Z", "worker", 10
    )
    assert len(first) == 1
    lease_id = first[0]["leaseId"]
    assert lease_id
    assert (
        await data_layer.claim_due_scheduled_tasks(
            "2020-01-01T00:00:00Z", "2020-01-01T00:01:00Z", "other", 10
        )
        == []
    )
    assert await data_layer.renew_scheduled_task_lease(
        task["id"], lease_id, "2020-01-01T00:02:00Z"
    )
    assert not await data_layer.complete_scheduled_task_run(
        task["id"],
        "stale",
        ScheduledTaskCompletion(status="failure", completedAt="2020-01-01T00:03:00Z"),
    )
    assert await data_layer.complete_scheduled_task_run(
        task["id"],
        lease_id,
        ScheduledTaskCompletion(
            status="success",
            completedAt="2020-01-01T00:03:00Z",
            nextRunAt="2020-01-02T00:00:00Z",
        ),
    )
    current = await data_layer.get_scheduled_task(task["id"])
    assert current["lastStatus"] == "success"
    assert current["leaseId"] is None
