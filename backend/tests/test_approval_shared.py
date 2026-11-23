import asyncio
import os
import uuid
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

import chainlit.approval as approval_module
from chainlit.approval import (
    Approval,
    ApprovalNotPending,
    supersede_thread_approvals,
    transition,
)
from chainlit.question import QuestionOption, ask
from chainlit.state_store import MemoryStateStore, RedisStateStore
from chainlit.step import Step
from tests.test_approval import FakeDataLayer


@pytest.fixture(autouse=True)
def clean_registry():
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()
    approval_module._deleted_threads.clear()
    approval_module._managed_thread_users.clear()
    approval_module._reservations.clear()
    yield
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()
    approval_module._deleted_threads.clear()
    approval_module._managed_thread_users.clear()
    approval_module._reservations.clear()


@pytest_asyncio.fixture(params=["memory", "redis"])
async def shared_store(request, monkeypatch):
    prefix = f"shared-test:{uuid.uuid4().hex}"
    if request.param == "memory":
        store = MemoryStateStore(prefix=prefix)
        store.shared = True
    else:
        url = os.getenv("CHAINLIT_TEST_REDIS_URL")
        if not url:
            pytest.skip("CHAINLIT_TEST_REDIS_URL is not set")
        store = RedisStateStore(url, prefix=prefix)
    monkeypatch.setattr("chainlit.approval.get_state_store", lambda: store)
    yield store
    await store.close()


@pytest.fixture
def layer(monkeypatch):
    fake = FakeDataLayer()
    monkeypatch.setattr("chainlit.approval.get_data_layer", lambda: fake)
    step_module = __import__("chainlit.step", fromlist=["get_data_layer"])
    monkeypatch.setattr(step_module, "get_data_layer", lambda: fake)
    return fake


async def _pending_ask(step: Step, arguments=None, timeout=30, **kwargs):
    before = set(approval_module._pending_by_approval)
    task = asyncio.create_task(
        Approval(
            step=step,
            arguments=arguments or {"amount": 1},
            timeout=timeout,
            **kwargs,
        ).ask()
    )
    for _ in range(200):
        new_ids = set(approval_module._pending_by_approval) - before
        if new_ids:
            return task, approval_module._pending_by_approval[new_ids.pop()]
        if task.done():
            raise AssertionError(f"ask failed early: {task.exception()!r}")
        await asyncio.sleep(0.01)
    raise AssertionError("approval entry was not registered")


async def _pending_question(step: Step, **kwargs):
    before = set(approval_module._pending_by_approval)
    task = asyncio.create_task(
        ask(
            "Which environment?", step=step, timeout=kwargs.pop("timeout", 30), **kwargs
        )
    )
    for _ in range(200):
        new_ids = set(approval_module._pending_by_approval) - before
        if new_ids:
            return task, approval_module._pending_by_approval[new_ids.pop()]
        if task.done():
            raise AssertionError(f"ask failed early: {task.exception()!r}")
        await asyncio.sleep(0.01)
    raise AssertionError("question entry was not registered")


async def _decide(session, entry, decision="approved", **kwargs):
    return await transition(
        entry.approval_id,
        decision,
        actor=session.user,
        thread_id=session.thread_id,
        session_generation=session.generation,
        **kwargs,
    )


async def _answer(session, entry, *, option_id=None, text=None):
    answer: Dict[str, Any] = {}
    if option_id is not None:
        answer["optionId"] = option_id
    if text is not None:
        answer["text"] = text
    return await transition(
        entry.approval_id,
        "answered",
        actor=session.user,
        thread_id=session.thread_id,
        session_generation=session.generation,
        expected_kind="question",
        answer=answer,
    )


async def test_remote_approval_decision_resolves_owner(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        # This is the other worker: no local waiter, only the shared record.
        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()

        record = await _decide(mock_session, entry)
        decision = await task

    assert record["status"] == "approved"
    assert decision.approved is True
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "approved"


async def test_remote_question_answer_resolves_owner(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-2"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="question", type="tool")
        await step.send()
        task, entry = await _pending_question(
            step,
            options=[
                QuestionOption("staging", "Staging"),
                QuestionOption("prod", "Production"),
            ],
        )

        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()

        record = await _answer(mock_session, entry, option_id="staging")
        answer = await task

    assert record["status"] == "answered"
    assert answer.option_id == "staging"
    assert answer.label == "Staging"


async def test_shared_liveness_and_managed_threads(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-live"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()
        approval_module._managed_thread_users.clear()

        assert entry.approval_id in await approval_module.live_interaction_ids(
            [{"id": step.thread_id}]
        )
        assert step.thread_id in await approval_module.shared_managed_thread_ids(
            mock_session.user.identifier
        )

        await shared_store.delete(approval_module._owner_key(entry.approval_id))
        assert entry.approval_id not in await approval_module.live_interaction_ids(
            [{"id": step.thread_id}]
        )

        with pytest.raises(ApprovalNotPending):
            await _decide(mock_session, entry, "rejected")
        await task


async def test_shared_guard_sees_a_pending_owner_on_another_worker(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-guard"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()

        with pytest.raises(approval_module.ApprovalPending):
            await approval_module.guard_pending_approval(step.thread_id)

        await _decide(mock_session, entry, "rejected")
        await task

    await approval_module.guard_pending_approval(step.thread_id)


async def test_shared_capacity_counts_pending_from_another_worker(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-cap"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        entries = []
        tasks = []
        for index in range(8):
            step = Step(name=f"tool-{index}", type="tool")
            await step.send()
            task, entry = await _pending_ask(step)
            entries.append(entry)
            tasks.append(task)

        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()

        ninth = Step(name="tool-8", type="tool")
        await ninth.send()
        with pytest.raises(approval_module.ApprovalLimitReached):
            await Approval(step=ninth, arguments={}).ask()

        for entry in entries:
            await _decide(mock_session, entry, "rejected")
        await asyncio.gather(*tasks)


async def test_remote_delete_supersedes_owner(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-3"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, _entry = await _pending_ask(step)

        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()

        await supersede_thread_approvals(step.thread_id, reason="deleted")
        decision = await task

    assert decision.decision == "superseded"
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "superseded"


async def test_remote_decision_orphans_when_owner_is_gone(
    shared_store, layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-shared-4"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        approval_module._pending_by_approval.clear()
        approval_module._pending_by_step.clear()
        await shared_store.delete(approval_module._owner_key(entry.approval_id))

        with pytest.raises(ApprovalNotPending) as refusal:
            await _decide(mock_session, entry)
        decision = await task

    assert refusal.value.record is not None
    assert refusal.value.record["status"] == "orphaned"
    assert decision.decision == "orphaned"
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "orphaned"
