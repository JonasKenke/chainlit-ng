"""Prompt scheduling validation, capability, and API tests."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from chainlit.auth import get_current_user
from chainlit.callbacks import after_scheduled_task
from chainlit.config import config
from chainlit.data.base import BaseDataLayer
from chainlit.scheduled_task import next_run_at, validate_cron
from chainlit.server import app
from chainlit.user import PersistedUser


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def user():
    return PersistedUser(id="user-1", identifier="one@example.com", createdAt="now")


@pytest.fixture
def task():
    return {
        "id": "task-1",
        "userId": "user-1",
        "userIdentifier": "one@example.com",
        "prompt": "hello",
        "chatProfile": None,
        "cron": "0 9 * * 1-5",
        "timezone": "UTC",
        "enabled": True,
        "afterHooks": [],
        "nextRunAt": "2099-01-01T09:00:00Z",
        "manualRunRequestedAt": None,
        "lastRunAt": None,
        "lastStatus": None,
        "lastError": None,
        "lastThreadId": None,
        "leaseId": None,
        "leaseUntil": None,
        "createdAt": "now",
        "updatedAt": "now",
    }


def setup(monkeypatch, user, layer):
    monkeypatch.setattr(config.features, "prompt_scheduling", True)
    monkeypatch.setattr("chainlit.server._supports_scheduled_tasks", lambda _: True)
    layer.get_user.return_value = user
    layer.request_scheduled_task_run.return_value = True
    app.dependency_overrides[get_current_user] = lambda: user
    return patch("chainlit.server.get_data_layer", return_value=layer)


def teardown():
    app.dependency_overrides.pop(get_current_user, None)


def test_after_hook_registration_rejects_duplicates(test_config):
    @after_scheduled_task("Notify", default=False)
    async def notify(_result):
        return None

    assert test_config.code.scheduled_task_after_hooks["Notify"]["default"] is False
    with pytest.raises(ValueError, match="already registered"):
        after_scheduled_task("Notify")(notify)


def test_cron_validation_and_timezone():
    assert validate_cron("0 9 * * 1-5") == "0 9 * * 1-5"
    with pytest.raises(ValueError, match="exactly five"):
        validate_cron("0 9 * * 1-5 2025")
    with pytest.raises(ValueError, match="numeric"):
        validate_cron("0 9 * * MON")
    with pytest.raises(ValueError, match="future occurrence"):
        validate_cron("0 0 31 2 *")
    assert next_run_at("0 9 * * 1-5", "America/New_York").tzinfo is not None


def test_dst_gap_moves_forward_and_fold_runs_once():
    spring = next_run_at(
        "30 2 * * *",
        "America/New_York",
        datetime(2025, 3, 8, 12, tzinfo=timezone.utc),
    )
    assert spring == datetime(2025, 3, 9, 7, tzinfo=timezone.utc)

    first = next_run_at(
        "30 1 * * *",
        "America/New_York",
        datetime(2025, 11, 1, 12, tzinfo=timezone.utc),
    )
    second = next_run_at("30 1 * * *", "America/New_York", first)
    assert first == datetime(2025, 11, 2, 5, 30, tzinfo=timezone.utc)
    assert second == datetime(2025, 11, 3, 6, 30, tzinfo=timezone.utc)


def test_disabled_and_unauthenticated(client, monkeypatch):
    monkeypatch.setattr(config.features, "prompt_scheduling", False)
    response = client.get("/project/scheduled-tasks")
    assert response.status_code == 400
    app.dependency_overrides[get_current_user] = lambda: None
    monkeypatch.setattr(config.features, "prompt_scheduling", True)
    try:
        assert client.get("/project/scheduled-tasks").status_code == 401
    finally:
        teardown()


def test_schedule_crud_and_run_now(client, monkeypatch, user, task):
    async def models(_user, _language):
        return ["🔷 Gemini 3.5 Flash"]

    monkeypatch.setattr(config.code, "set_scheduled_task_models", models)
    layer = AsyncMock(spec=BaseDataLayer)
    layer.list_scheduled_tasks.return_value = [task]
    layer.create_scheduled_task.side_effect = lambda value: value
    layer.get_scheduled_task.return_value = task
    layer.update_scheduled_task.side_effect = lambda value: value
    layer.delete_scheduled_task.return_value = True
    with setup(monkeypatch, user, layer):
        assert client.get("/project/scheduled-tasks").json()[0]["id"] == "task-1"
        created = client.post(
            "/project/scheduled-tasks",
            json={
                "prompt": "new",
                "model": "🔷 Gemini 3.5 Flash",
                "cron": "0 9 * * 1-5",
                "timezone": "UTC",
            },
        )
        assert created.status_code == 200
        assert created.json()["userId"] == "user-1"
        assert created.json()["model"] == "🔷 Gemini 3.5 Flash"
        assert (
            client.put(
                "/project/scheduled-tasks/task-1", json={"enabled": False}
            ).status_code
            == 200
        )
        assert client.post("/project/scheduled-tasks/task-1/run").status_code == 202
        assert client.delete("/project/scheduled-tasks/task-1").status_code == 200
    teardown()


def test_owner_and_validation_rejection(client, monkeypatch, user, task):
    layer = AsyncMock(spec=BaseDataLayer)
    layer.get_user.return_value = user
    layer.get_scheduled_task.return_value = {**task, "userId": "other"}
    with setup(monkeypatch, user, layer):
        assert (
            client.put(
                "/project/scheduled-tasks/task-1", json={"prompt": "hack"}
            ).status_code
            == 404
        )
        invalid = client.post(
            "/project/scheduled-tasks",
            json={"prompt": "", "cron": "bad", "timezone": "No/Such"},
        )
        assert invalid.status_code == 422
    teardown()


def test_model_must_be_declared_by_model_callback(client, monkeypatch, user, task):
    async def models(_user, _language):
        return ["allowed"]

    monkeypatch.setattr(config.code, "set_scheduled_task_models", models)
    layer = AsyncMock(spec=BaseDataLayer)
    layer.get_user.return_value = user
    layer.get_scheduled_task.return_value = task
    with setup(monkeypatch, user, layer):
        for response in (
            client.post(
                "/project/scheduled-tasks",
                json={
                    "prompt": "hello",
                    "model": "not-allowed",
                    "cron": "0 9 * * 1-5",
                    "timezone": "UTC",
                },
            ),
            client.put(
                "/project/scheduled-tasks/task-1",
                json={"model": "not-allowed"},
            ),
        ):
            assert response.status_code == 422
            assert response.json()["detail"] == "Unknown scheduled task model"
    teardown()


def test_model_requires_model_callback(client, monkeypatch, user):
    monkeypatch.setattr(config.code, "set_scheduled_task_models", None)
    layer = AsyncMock(spec=BaseDataLayer)
    with setup(monkeypatch, user, layer):
        response = client.post(
            "/project/scheduled-tasks",
            json={
                "prompt": "hello",
                "model": "arbitrary",
                "cron": "0 9 * * 1-5",
                "timezone": "UTC",
            },
        )
    teardown()
    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown scheduled task model"
