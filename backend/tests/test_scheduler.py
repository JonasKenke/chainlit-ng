"""Scheduler worker execution tests."""

from unittest.mock import AsyncMock

import pytest

from chainlit.config import config
from chainlit.message import Message
from chainlit.scheduler import ScheduledTaskWorker
from chainlit.types import ChatProfile
from chainlit.user import PersistedUser


@pytest.fixture
def task():
    return {
        "id": "task-1",
        "userId": "user-1",
        "userIdentifier": "one@example.com",
        "prompt": "hello",
        "chatProfile": None,
        "cron": "* * * * *",
        "timezone": "UTC",
        "enabled": True,
        "afterHooks": ["notify", "missing"],
        "nextRunAt": "2000-01-01T00:00:00Z",
        "manualRunRequestedAt": None,
        "lastRunAt": None,
        "lastStatus": None,
        "lastError": None,
        "lastThreadId": None,
        "leaseId": "worker:lease",
        "leaseUntil": "2099-01-01T00:00:00Z",
        "createdAt": "2000-01-01T00:00:00Z",
        "updatedAt": "2000-01-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_run_executes_normal_callbacks_and_isolates_hook_failure(
    task, monkeypatch
):
    layer = AsyncMock()
    layer.get_user.return_value = PersistedUser(
        id="user-1", identifier="one@example.com", createdAt="now"
    )
    layer.renew_scheduled_task_lease.return_value = True
    layer.complete_scheduled_task_run.return_value = True
    start = AsyncMock()

    async def send_output(_message):
        await Message(content="answer").send()

    message = AsyncMock(side_effect=send_output)
    hook = AsyncMock(side_effect=RuntimeError("hook failed"))
    old = (
        config.code.on_chat_start,
        config.code.on_message,
        config.code.scheduled_task_after_hooks,
    )
    config.code.on_chat_start = start
    config.code.on_message = message
    config.code.scheduled_task_after_hooks = {
        "notify": {"name": "notify", "default": True, "callback": hook}
    }
    monkeypatch.setattr("chainlit.message.get_data_layer", lambda: None)
    try:
        await ScheduledTaskWorker(layer).run_task(task)
    finally:
        (
            config.code.on_chat_start,
            config.code.on_message,
            config.code.scheduled_task_after_hooks,
        ) = old

    start.assert_awaited_once()
    message.assert_awaited_once()
    hook.assert_awaited_once()
    layer.update_thread.assert_awaited_once()
    completion = layer.complete_scheduled_task_run.await_args.args[2]
    assert completion.status == "success_with_warnings"
    assert completion.lastThreadId
    assert hook.await_args.args[0].outputMessages == ["answer"]


@pytest.mark.asyncio
async def test_run_applies_saved_model_setting(task, monkeypatch):
    layer = AsyncMock()
    layer.get_user.return_value = PersistedUser(
        id="user-1", identifier="one@example.com", createdAt="now"
    )
    layer.complete_scheduled_task_run.return_value = True
    settings_update = AsyncMock()
    message = AsyncMock()
    old = (
        config.code.on_chat_start,
        config.code.on_message,
        config.code.on_settings_update,
    )
    config.code.on_chat_start = None
    config.code.on_message = message
    config.code.on_settings_update = settings_update
    monkeypatch.setattr("chainlit.message.get_data_layer", lambda: None)
    try:
        await ScheduledTaskWorker(layer).run_task(
            {**task, "model": "🔷 Gemini 3.5 Flash"}
        )
    finally:
        (
            config.code.on_chat_start,
            config.code.on_message,
            config.code.on_settings_update,
        ) = old

    settings_update.assert_awaited_once_with({"model": "🔷 Gemini 3.5 Flash"})


@pytest.mark.asyncio
async def test_removed_profile_fails_and_pauses(task):
    layer = AsyncMock()
    layer.get_user.return_value = PersistedUser(
        id="user-1", identifier="one@example.com", createdAt="now"
    )
    old_profiles = config.code.set_chat_profiles

    async def profiles(_user, _language):
        return [ChatProfile(name="current", markdown_description="current")]

    config.code.set_chat_profiles = profiles
    task = {**task, "chatProfile": "removed"}
    try:
        await ScheduledTaskWorker(layer).run_task(task)
    finally:
        config.code.set_chat_profiles = old_profiles

    completion = layer.complete_scheduled_task_run.await_args.args[2]
    assert completion.status == "failure"
    assert completion.enabled is False


@pytest.mark.asyncio
async def test_run_failure_still_completes_and_runs_hooks(task, monkeypatch):
    layer = AsyncMock()
    layer.get_user.return_value = PersistedUser(
        id="user-1", identifier="one@example.com", createdAt="now"
    )
    layer.complete_scheduled_task_run.return_value = True
    failure = AsyncMock(side_effect=RuntimeError("callback failed"))
    hook = AsyncMock()
    old = (
        config.code.on_chat_start,
        config.code.on_message,
        config.code.scheduled_task_after_hooks,
    )
    config.code.on_chat_start = None
    config.code.on_message = failure
    config.code.scheduled_task_after_hooks = {
        "notify": {"name": "notify", "default": True, "callback": hook}
    }
    monkeypatch.setattr("chainlit.message.get_data_layer", lambda: None)
    try:
        await ScheduledTaskWorker(layer).run_task(task)
    finally:
        (
            config.code.on_chat_start,
            config.code.on_message,
            config.code.scheduled_task_after_hooks,
        ) = old

    failure.assert_awaited_once()
    hook.assert_awaited_once()
    completion = layer.complete_scheduled_task_run.await_args.args[2]
    assert completion.status == "failure"
    assert completion.lastError == "Scheduled task execution failed"
