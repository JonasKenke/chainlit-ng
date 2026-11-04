"""Storage-backed scheduled prompt worker."""

import asyncio
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Optional

from chainlit.chat_context import chat_context, chat_contexts
from chainlit.context import init_http_context
from chainlit.data.base import BaseDataLayer
from chainlit.logger import logger
from chainlit.message import ErrorMessage, Message
from chainlit.scheduled_task import next_run_at
from chainlit.types import (
    ScheduledTaskCompletion,
    ScheduledTaskDict,
    ScheduledTaskResult,
)
from chainlit.user_session import user_sessions
from chainlit.utils import utc_now


class ScheduledTaskWorker:
    def __init__(
        self,
        data_layer: BaseDataLayer,
        poll_interval: float = 5.0,
        lease_seconds: int = 60,
        limit: int = 10,
        concurrency: int = 4,
        shutdown_timeout: float = 10.0,
    ):
        self.data_layer = data_layer
        self.poll_interval = poll_interval
        self.lease_seconds = lease_seconds
        self.limit = limit
        self.concurrency = max(1, concurrency)
        self.shutdown_timeout = shutdown_timeout
        self.worker_id = str(uuid.uuid4())
        self.stop_event = asyncio.Event()
        self.active: set[asyncio.Task] = set()

    async def _heartbeat(self, task_id: str, lease_id: str) -> None:
        while True:
            await asyncio.sleep(max(1.0, self.lease_seconds / 2))
            until = datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)
            renewed = await self.data_layer.renew_scheduled_task_lease(
                task_id,
                lease_id,
                until.isoformat().replace("+00:00", "Z"),
            )
            if not renewed:
                return

    async def poll_once(self) -> list[asyncio.Task]:
        available = self.concurrency - len(self.active)
        if available <= 0:
            return []
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(seconds=self.lease_seconds)
        tasks = await self.data_layer.claim_due_scheduled_tasks(
            now=now.isoformat().replace("+00:00", "Z"),
            lease_until=lease_until.isoformat().replace("+00:00", "Z"),
            worker_id=self.worker_id,
            limit=min(self.limit, available),
        )
        created = []
        for task in tasks[:available]:
            created_task = asyncio.create_task(self.run_task(task))
            self.active.add(created_task)
            created_task.add_done_callback(self.active.discard)
            created.append(created_task)
        return created

    async def run_task(self, task: ScheduledTaskDict) -> None:
        lease_id = task["leaseId"]
        if not lease_id:
            return
        started = utc_now()
        run_at = task["manualRunRequestedAt"] or task["nextRunAt"]
        run_id = f"{task['id']}:{run_at}"
        thread_id: Optional[str] = None
        session_id: Optional[str] = None
        status = "success"
        error: Optional[str] = None
        paused = False
        heartbeat = asyncio.create_task(self._heartbeat(task["id"], lease_id))
        current_task = asyncio.current_task()

        def cleanup(_completed_task: asyncio.Task) -> None:
            if session_id:
                chat_contexts.pop(session_id, None)
                user_sessions.pop(session_id, None)
            if not heartbeat.done():
                heartbeat.cancel()

        if current_task:
            current_task.add_done_callback(cleanup)
        from chainlit.config import config

        try:
            persisted = await self.data_layer.get_user(task["userIdentifier"])
            if not persisted or persisted.id != task["userId"]:
                raise ValueError("Scheduled task owner is no longer available")

            effective_config = config
            if task["chatProfile"]:
                if not config.code.set_chat_profiles:
                    paused = True
                    raise ValueError("Scheduled task chat profile is unavailable")
                profiles = await config.code.set_chat_profiles(persisted, None)
                profile = next(
                    (item for item in profiles if item.name == task["chatProfile"]),
                    None,
                )
                if profile is None:
                    paused = True
                    raise ValueError("Scheduled task chat profile is unavailable")
                if profile.config_overrides:
                    effective_config = config.with_overrides(profile.config_overrides)

            thread_id = str(uuid.uuid4())
            chainlit_context = init_http_context(
                thread_id=thread_id,
                user=persisted,
                chat_profile=task["chatProfile"],
                persist_thread=False,
            )
            session_id = chainlit_context.session.id
            chainlit_context.session.raise_callback_errors = True
            chainlit_context.session.config = effective_config
            model = task.get("model")
            if model:
                chainlit_context.session.chat_settings["model"] = model
            await self.data_layer.update_thread(
                thread_id=thread_id,
                user_id=task["userId"],
                metadata={
                    "scheduledTaskId": task["id"],
                    "scheduledRunId": run_id,
                    "scheduledFor": run_at,
                    "chat_profile": task["chatProfile"],
                },
            )
            if config.code.on_chat_start:
                await config.code.on_chat_start()
            if model and config.code.on_settings_update:
                await config.code.on_settings_update({"model": model})
            message = Message(
                content=task["prompt"],
                author=task["userIdentifier"],
                type="user_message",
                metadata={
                    "scheduledTaskId": task["id"],
                    "scheduledRunId": run_id,
                    "scheduledFor": run_at,
                    "chat_profile": task["chatProfile"],
                },
            )
            await message.send()
            if config.code.on_message:
                await config.code.on_message(message)
        except Exception:
            status = "failure"
            error = "Scheduled task execution failed"
            logger.exception("Scheduled task %s failed", task["id"])
            if session_id:
                try:
                    await ErrorMessage(content=error, author="Error").send()
                except Exception:
                    logger.exception("Unable to persist scheduled task failure message")

        output_messages = (
            [
                message.content
                for message in chat_context.get()
                if message.type != "user_message"
            ]
            if session_id
            else []
        )
        result = ScheduledTaskResult(
            taskId=task["id"],
            runId=run_id,
            threadId=thread_id,
            owner=task["userIdentifier"],
            prompt=task["prompt"],
            chatProfile=task["chatProfile"],
            model=task.get("model"),
            scheduledFor=run_at,
            startedAt=started,
            completedAt=utc_now(),
            status=status,
            outputMessages=output_messages,
            error=error,
        )
        for hook_name in task["afterHooks"]:
            hook = config.code.scheduled_task_after_hooks.get(hook_name)
            if not hook:
                continue
            try:
                await hook["callback"](result)
            except Exception:
                if result.status == "success":
                    result.status = "success_with_warnings"
                result.error = result.error or "One or more scheduled task hooks failed"
                logger.exception("Scheduled task hook %s failed", hook_name)

        next_run = None
        if task["enabled"] and not task["manualRunRequestedAt"] and not paused:
            try:
                next_run = (
                    next_run_at(
                        task["cron"],
                        task["timezone"],
                        after=datetime.now(timezone.utc),
                    )
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except ValueError:
                result.status = "failure"
                result.error = "Scheduled task recurrence is invalid"
        await self.data_layer.complete_scheduled_task_run(
            task["id"],
            lease_id,
            ScheduledTaskCompletion(
                status=result.status,
                completedAt=utc_now(),
                lastError=result.error,
                lastThreadId=thread_id,
                nextRunAt=next_run,
                enabled=False if paused else None,
            ),
        )
        if session_id:
            chat_contexts.pop(session_id, None)
            user_sessions.pop(session_id, None)
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled task poll failed")
            try:
                await asyncio.wait_for(self.stop_event.wait(), self.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def shutdown(self) -> None:
        self.stop_event.set()
        if not self.active:
            return
        done, pending = await asyncio.wait(self.active, timeout=self.shutdown_timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)
