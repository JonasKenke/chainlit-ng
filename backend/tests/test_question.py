import asyncio
import importlib
from typing import Any, Dict
from unittest.mock import AsyncMock

import httpx
import pytest

import chainlit.approval as approval_module
from chainlit.approval import (
    Approval,
    ApprovalAuthRequired,
    ApprovalLimitReached,
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalNotReady,
    ApprovalPending,
    ApprovalPersistenceError,
    ApprovalUnsupported,
    QuestionAlreadyAsked,
    QuestionInvalid,
    QuestionOption,
    ask,
    guard_pending_approval,
    is_thread_deleted,
    mark_thread_deleted,
    supersede_thread_approvals,
    transition,
)
from chainlit.auth import get_current_user
from chainlit.server import app
from chainlit.step import Step
from chainlit.user import User
from tests.test_approval import FakeDataLayer

PROMPT = "Which environment should I deploy to?"


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


@pytest.fixture
def layer(monkeypatch):
    fake = FakeDataLayer()
    monkeypatch.setattr("chainlit.approval.get_data_layer", lambda: fake)
    step_module = importlib.import_module("chainlit.step")
    monkeypatch.setattr(step_module, "get_data_layer", lambda: fake)
    server_module = importlib.import_module("chainlit.server")
    monkeypatch.setattr(server_module, "get_data_layer", lambda: fake)
    return fake


async def _pending_question(step: Step, **kwargs):
    before = set(approval_module._pending_by_approval)
    task = asyncio.create_task(
        ask(PROMPT, step=step, timeout=kwargs.pop("timeout", 30), **kwargs)
    )
    for _ in range(200):
        new_ids = set(approval_module._pending_by_approval) - before
        if new_ids:
            return task, approval_module._pending_by_approval[new_ids.pop()]
        if task.done():
            raise AssertionError(f"ask failed early: {task.exception()!r}")
        await asyncio.sleep(0.01)
    raise AssertionError("question entry was not registered")


async def test_pending_question_blocks_a_new_run(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context:
        step = Step(name="question-step", type="tool")
        await step.send()
        task, entry = await _pending_question(step)

        with pytest.raises(ApprovalPending):
            guard_pending_approval(step.thread_id)
        assert guard_pending_approval("another-thread") is None

        await _answer(mock_session, entry, text="staging")
        await task

    guard_pending_approval(step.thread_id)


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


async def test_option_answer_is_returned_and_persisted(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(
            step,
            options=[
                QuestionOption("staging", "Staging", "Pre-production"),
                QuestionOption("prod", "Production"),
            ],
        )

        record = step.metadata["approval"]
        assert record["kind"] == "question"
        assert record["status"] == "pending"
        assert record["prompt"] == PROMPT
        assert [o["id"] for o in record["options"]] == ["staging", "prod"]
        assert layer.steps[step.id]["metadata"]["approval"]["id"] == entry.approval_id

        emitted = ctx.emitter.send_question.await_args.args[0]
        assert emitted["kind"] == "question"
        assert emitted["status"] == "pending"
        assert "tool" not in emitted
        ctx.emitter.send_approval.assert_not_called()

        await _answer(mock_session, entry, option_id="staging")
        answer = await task

    assert answer.answered is True
    assert answer.option_id == "staging"
    assert answer.label == "Staging"
    assert answer.text is None
    assert answer.durable is True
    stored = layer.steps[step.id]["metadata"]["approval"]
    assert stored["status"] == "answered"
    assert stored["answer"] == {"optionId": "staging", "label": "Staging"}
    assert stored["decidedBy"] == mock_session.user.identifier
    terminal = ctx.emitter.send_question.await_args.args[0]
    assert terminal["status"] == "answered"
    assert terminal["answer"] == {"optionId": "staging", "label": "Staging"}
    ctx.emitter.send_approval.assert_not_called()


async def test_free_text_answer_is_normalized_and_persisted(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)
        await _answer(mock_session, entry, text="  blue\r\ngreen  ")
        answer = await task

    assert answer.status == "answered"
    assert answer.text == "blue\ngreen"
    assert answer.option_id is None
    assert layer.steps[step.id]["metadata"]["approval"]["answer"] == {
        "text": "blue\ngreen"
    }


async def test_invalid_question_input_is_refused_before_any_write(layer):
    with pytest.raises(QuestionInvalid):
        await ask("")
    with pytest.raises(QuestionInvalid):
        await ask("question", allow_free_text=False)
    with pytest.raises(QuestionInvalid):
        await ask(
            "question",
            options=[QuestionOption(f"o{i}", f"Option {i}") for i in range(9)],
        )
    with pytest.raises(QuestionInvalid):
        await ask(
            "question",
            options=[
                QuestionOption("same", "One"),
                QuestionOption("same", "Two"),
            ],
        )
    assert layer.write_calls == 0


async def test_both_or_neither_answer_is_refused_without_a_write(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step, options=[QuestionOption("a", "A")])

        with pytest.raises(QuestionInvalid):
            await _answer(mock_session, entry, option_id="a", text="text")
        with pytest.raises(QuestionInvalid):
            await transition(
                entry.approval_id,
                "answered",
                actor=mock_session.user,
                thread_id=mock_session.thread_id,
                session_generation=mock_session.generation,
                expected_kind="question",
                answer={},
            )
        with pytest.raises(QuestionInvalid):
            await _answer(mock_session, entry, option_id="unknown")

        assert step.metadata["approval"]["status"] == "pending"
        assert entry.approval_id in approval_module._pending_by_approval
        assert task.done() is False

        await _answer(mock_session, entry, option_id="a")
        await task


async def test_timeout_returns_expired_and_not_durable(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        answer = await ask(PROMPT, step=step, timeout=0.02)

    assert answer.status == "expired"
    assert answer.durable is False
    assert step.metadata["approval"]["terminalReason"] == "timeout"
    assert not approval_module._pending_by_approval


async def test_stop_returns_superseded_and_not_durable(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, _ = await _pending_question(step)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert step.metadata["approval"]["status"] == "superseded"
    assert step.metadata["approval"]["terminalReason"] == "stopped"
    assert not approval_module._pending_by_approval


async def test_second_pending_ask_supersedes_the_first(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        first_task, first = await _pending_question(step)
        second_task, second = await _pending_question(step)

        first_answer = await first_task
        assert first_answer.status == "superseded"
        assert first.thread_id == second.thread_id
        assert second.approval_id in approval_module._pending_by_approval

        await _answer(mock_session, second, option_id=None, text="go")
        await second_task

    assert step.metadata["approval"]["id"] == second.approval_id
    assert step.metadata["approval"]["status"] == "answered"


async def test_second_ask_after_a_terminal_question_is_refused(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)
        await _answer(mock_session, entry, text="first")
        await task

        with pytest.raises(QuestionAlreadyAsked):
            await ask(PROMPT, step=step, timeout=1)

    assert step.metadata["approval"]["answer"] == {"text": "first"}


async def test_late_answer_returns_the_terminal_record_and_changes_nothing(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)
        record = await _answer(mock_session, entry, text="first")
        await task

        with pytest.raises(ApprovalNotPending) as exc:
            await _answer(mock_session, entry, text="second")
        stored = step.metadata["approval"]

    assert exc.value.record["answer"] == {"text": "first"}
    assert stored["revision"] == record["revision"]
    assert stored["answer"] == {"text": "first"}


async def test_terminal_memo_after_thread_delete_is_not_found(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)
        await _answer(mock_session, entry, text="secret")
        await task

        mark_thread_deleted(step.thread_id)
        with pytest.raises(ApprovalNotFound):
            await _answer(mock_session, entry, text="secret")


async def test_ninth_pending_ask_raises_before_any_write(
    layer, mock_chainlit_context, mock_session, monkeypatch
):
    monkeypatch.setattr(approval_module, "MAX_PENDING_INTERACTIONS_PER_SESSION", 2)
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        tasks = []
        for index in range(2):
            step = Step(name=f"deploy-{index}", type="tool")
            await step.send()
            tasks.append((step, await _pending_question(step)))

        third = Step(name="deploy-2", type="tool")
        await third.send()
        writes_before = layer.write_calls
        with pytest.raises(ApprovalLimitReached):
            await ask(PROMPT, step=third)

        assert layer.write_calls == writes_before
        assert "approval" not in third.metadata
        assert approval_module._reservations.get("gen-1") is None

        for _, (task, entry) in tasks:
            await _answer(mock_session, entry, text="done")
            await task


async def test_nine_concurrent_asks_reserve_at_most_eight_slots(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    layer.write_delay = 0.05

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        steps = []
        for index in range(9):
            step = Step(name=f"deploy-{index}", type="tool")
            await step.send()
            steps.append(step)

        tasks = [
            asyncio.create_task(ask(PROMPT, step=step, timeout=30)) for step in steps
        ]
        for _ in range(200):
            if sum(1 for task in tasks if task.done()) >= 1:
                break
            await asyncio.sleep(0.01)

        pending = [
            entry
            for entry in approval_module._pending_by_approval.values()
            if entry.session_generation == "gen-1"
        ]
        assert len(pending) <= 8
        refused = [task for task in tasks if task.done()]
        assert len(refused) == 1
        assert isinstance(refused[0].exception(), ApprovalLimitReached)

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert len(approval_module._pending_by_approval) == 0
    assert not approval_module._reservations


async def test_reservation_is_released_when_the_write_fails(
    layer, mock_chainlit_context, mock_session, monkeypatch
):
    monkeypatch.setattr(approval_module, "MAX_PENDING_INTERACTIONS_PER_SESSION", 1)
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    layer.fail_writes = 1

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()

        with pytest.raises(ApprovalPersistenceError):
            await ask(PROMPT, step=step)

        assert approval_module._reservations.get("gen-1") is None
        assert "approval" not in step.metadata


async def test_reservation_is_released_when_the_ask_is_cancelled(
    layer, mock_chainlit_context, mock_session, monkeypatch
):
    monkeypatch.setattr(approval_module, "MAX_PENDING_INTERACTIONS_PER_SESSION", 1)
    monkeypatch.setattr(approval_module, "MAX_PENDING_APPROVALS_PER_SESSION", 1)
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    layer.write_delay = 0.2

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        first = Step(name="deploy", type="tool")
        await first.send()
        task = asyncio.create_task(ask(PROMPT, step=first, timeout=30))
        for _ in range(100):
            if approval_module._reservations.get("gen-1"):
                break
            await asyncio.sleep(0.01)
        assert approval_module._reservations.get("gen-1") == 1

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert approval_module._reservations.get("gen-1") is None

        layer.write_delay = 0
        second = Step(name="deploy-2", type="tool")
        await second.send()
        other_task, other_entry = await _pending_question(second)
        await _answer(mock_session, other_entry, text="go")
        await other_task


async def test_auth_is_required_before_any_write(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    async with mock_chainlit_context:
        mock_session.user = None
        step = Step(name="deploy", type="tool")
        with pytest.raises(ApprovalAuthRequired):
            await ask(PROMPT, step=step)

    assert layer.write_calls == 0
    assert "approval" not in step.metadata


async def test_first_user_message_is_required_before_any_write(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = False

    async with mock_chainlit_context:
        step = Step(name="deploy", type="tool")
        with pytest.raises(ApprovalNotReady):
            await ask(PROMPT, step=step)

    assert layer.write_calls == 0
    assert "approval" not in step.metadata


async def test_answer_uses_the_ask_time_snapshot_not_mutated_metadata(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(
            step, options=[QuestionOption("a", "Original A")]
        )

        # The app mutates the slot after the ask; the snapshot must win.
        step.metadata["approval"]["options"] = [
            {"id": "evil", "label": "Injected", "description": None}
        ]
        await _answer(mock_session, entry, option_id="a")
        answer = await task

        assert answer.option_id == "a"
        assert answer.label == "Original A"
        assert step.metadata["approval"]["answer"] == {
            "optionId": "a",
            "label": "Original A",
        }


async def test_inplace_option_append_is_not_accepted(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(
            step, options=[QuestionOption("a", "Original A")]
        )

        step.metadata["approval"]["options"].append(
            {"id": "evil", "label": "Injected", "description": None}
        )
        with pytest.raises(QuestionInvalid):
            await _answer(mock_session, entry, option_id="evil")
        assert step.metadata["approval"]["status"] == "pending"

        await _answer(mock_session, entry, option_id="a")
        answer = await task

    assert answer.label == "Original A"


async def test_inplace_label_mutation_cannot_change_the_stored_label(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(
            step, options=[QuestionOption("a", "Original A")]
        )

        step.metadata["approval"]["options"][0]["label"] = "Hacked"
        await _answer(mock_session, entry, option_id="a")
        answer = await task

        assert answer.label == "Original A"
        assert step.metadata["approval"]["answer"] == {
            "optionId": "a",
            "label": "Original A",
        }


async def test_cross_kind_transitions_are_refused(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        ctx.emitter.send_approval = AsyncMock()

        step = Step(name="deploy", type="tool")
        await step.send()
        task, question_entry = await _pending_question(step)

        with pytest.raises(ValueError, match="Cannot transition"):
            await transition(
                question_entry.approval_id,
                "approved",
                actor=mock_session.user,
                expected_kind="question",
            )
        with pytest.raises(ApprovalNotFound):
            await transition(
                question_entry.approval_id,
                "answered",
                actor=mock_session.user,
                expected_kind="approval",
                answer={"text": "no"},
            )

        await _answer(mock_session, question_entry, text="yes")
        await task

        with pytest.raises(ApprovalNotFound):
            await transition(
                question_entry.approval_id,
                "answered",
                actor=mock_session.user,
                expected_kind="approval",
                answer={"text": "no"},
            )

        approval_step = Step(name="dangerous", type="tool")
        await approval_step.send()
        approval_task = asyncio.create_task(
            Approval(step=approval_step, arguments={"x": 1}, timeout=30).ask()
        )
        for _ in range(200):
            approval_id = approval_module._pending_by_step.get(
                (approval_step.thread_id, approval_step.id)
            )
            if approval_id:
                break
            await asyncio.sleep(0.01)
        approval_entry = approval_module._pending_by_approval[approval_id]

        with pytest.raises(ValueError, match="Cannot transition"):
            await transition(
                approval_entry.approval_id,
                "answered",
                actor=mock_session.user,
                expected_kind="approval",
                answer={"text": "no"},
            )
        with pytest.raises(ApprovalNotFound):
            await transition(
                approval_entry.approval_id,
                "approved",
                actor=mock_session.user,
                expected_kind="question",
            )

        await transition(
            approval_entry.approval_id,
            "approved",
            actor=mock_session.user,
            expected_kind="approval",
        )
        await approval_task

        with pytest.raises(ApprovalNotFound):
            await transition(
                approval_entry.approval_id,
                "answered",
                actor=mock_session.user,
                expected_kind="question",
                answer={"text": "no"},
            )


async def test_question_terminal_event_never_builds_the_approval_payload(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)
        await _answer(mock_session, entry, text="go")
        await task

        statuses = [
            call.args[0]["status"] for call in ctx.emitter.send_question.await_args_list
        ]
        assert statuses == ["pending", "answered"]
        ctx.emitter.send_approval.assert_not_called()


async def test_delete_supersedes_without_resurrecting_the_step(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, _ = await _pending_question(step)

        mark_thread_deleted(step.thread_id)
        await layer.delete_thread(step.thread_id)
        writes_after_delete = layer.write_calls
        await supersede_thread_approvals(step.thread_id)
        answer = await task

        await step.update()
        await step.send()

    assert answer.status == "superseded"
    assert answer.durable is False
    assert is_thread_deleted(step.thread_id)
    assert layer.write_calls == writes_after_delete
    assert step.id not in layer.steps


async def test_failed_answer_write_raises_and_keeps_pending(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)

        layer.fail_writes = 1
        with pytest.raises(ApprovalPersistenceError):
            await _answer(mock_session, entry, text="first")

        assert task.done() is False
        assert step.metadata["approval"]["status"] == "pending"
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"
        assert entry.approval_id in approval_module._pending_by_approval

        await _answer(mock_session, entry, text="retry")
        answer = await task

    assert answer.text == "retry"
    assert answer.durable is True


async def test_failed_timeout_write_returns_expired_not_durable(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, _ = await _pending_question(step, timeout=0.02)
        layer.fail_writes = 10
        answer = await task

    assert answer.status == "expired"
    assert answer.durable is False
    assert step.metadata["approval"]["status"] == "pending"


async def _post(
    monkeypatch,
    path,
    payload,
    user,
    session,
    headers=None,
    base_url="http://testserver",
):
    server_module = importlib.import_module("chainlit.server")

    class _SessionLookup:
        @staticmethod
        def get_by_id(session_id):
            return session

    monkeypatch.setattr(server_module, "WebsocketSession", _SessionLookup)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            return await client.post(
                path,
                json=payload,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _question_payload(session, entry, **kwargs):
    return {
        "sessionId": session.id,
        "questionId": entry.approval_id,
        "decisionRequestId": "request-1",
        **kwargs,
    }


async def test_route_answers_a_question(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(
            step, options=[QuestionOption("staging", "Staging")]
        )

        response = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, optionId="staging"),
            mock_session.user,
            mock_session,
            {"Origin": "http://testserver"},
        )
        answer = await task

    assert response.status_code == 200
    body = response.json()
    assert body["questionId"] == entry.approval_id
    assert body["status"] == "answered"
    assert body["answer"] == {"optionId": "staging", "label": "Staging"}
    assert body["durable"] is True
    assert "requestedBy" not in body
    assert answer.label == "Staging"


async def test_route_answers_with_free_text(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)

        response = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, text="blue"),
            mock_session.user,
            mock_session,
        )
        answer = await task

    assert response.status_code == 200
    assert response.json()["answer"] == {"text": "blue"}
    assert answer.text == "blue"


async def test_route_rejects_wrong_user_and_foreign_origin(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)

        wrong = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, text="no"),
            User(identifier="someone_else"),
            mock_session,
        )
        cross_origin = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, text="no"),
            mock_session.user,
            mock_session,
            {"Origin": "http://evil.example"},
        )
        assert wrong.status_code == 403
        assert cross_origin.status_code == 403
        assert task.done() is False

        await _answer(mock_session, entry, text="yes")
        await task


async def test_route_rejects_foreign_thread_and_generation(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)

        mock_session.thread_id = "other-thread"
        foreign_thread = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, text="no"),
            mock_session.user,
            mock_session,
        )
        mock_session.thread_id = step.thread_id

        mock_session.generation = "other-generation"
        foreign_generation = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, text="no"),
            mock_session.user,
            mock_session,
        )
        mock_session.generation = "gen-1"

        assert foreign_thread.status_code == 404
        assert foreign_generation.status_code == 403
        assert task.done() is False

        await _answer(mock_session, entry, text="yes")
        await task


async def test_route_rejects_both_or_neither_answer(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)

        both = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, optionId="a", text="b"),
            mock_session.user,
            mock_session,
        )
        neither = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry),
            mock_session.user,
            mock_session,
        )
        assert both.status_code == 422
        assert neither.status_code == 422
        assert task.done() is False

        await _answer(mock_session, entry, text="yes")
        await task


async def test_route_duplicate_returns_409_with_terminal_record(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        step = Step(name="deploy", type="tool")
        await step.send()
        task, entry = await _pending_question(step)
        await _answer(mock_session, entry, text="first")
        await task

        response = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, entry, text="second"),
            mock_session.user,
            mock_session,
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "QuestionNotPending"
    assert body["status"] == "answered"
    assert body["answer"] == {"text": "first"}


async def test_routes_refuse_cross_kind_ids(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_question = AsyncMock()
        ctx.emitter.send_approval = AsyncMock()

        question_step = Step(name="deploy", type="tool")
        await question_step.send()
        question_task, question_entry = await _pending_question(question_step)

        approval_step = Step(name="dangerous", type="tool")
        await approval_step.send()
        approval_task = asyncio.create_task(
            Approval(step=approval_step, arguments={"x": 1}, timeout=30).ask()
        )
        for _ in range(200):
            approval_id = approval_module._pending_by_step.get(
                (approval_step.thread_id, approval_step.id)
            )
            if approval_id:
                break
            await asyncio.sleep(0.01)
        approval_entry = approval_module._pending_by_approval[approval_id]

        approval_route = await _post(
            monkeypatch,
            "/project/approval",
            {
                "sessionId": mock_session.id,
                "approvalId": question_entry.approval_id,
                "decision": "approve",
                "decisionRequestId": "request-1",
            },
            mock_session.user,
            mock_session,
        )
        question_route = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, approval_entry, text="no"),
            mock_session.user,
            mock_session,
        )
        assert approval_route.status_code == 404
        assert question_route.status_code == 404
        assert "approvalId" not in approval_route.json()
        assert "questionId" not in question_route.json()

        await _answer(mock_session, question_entry, text="yes")
        await question_task
        await transition(
            approval_entry.approval_id,
            "approved",
            actor=mock_session.user,
            expected_kind="approval",
        )
        await approval_task

        terminal_approval_route = await _post(
            monkeypatch,
            "/project/approval",
            {
                "sessionId": mock_session.id,
                "approvalId": question_entry.approval_id,
                "decision": "approve",
                "decisionRequestId": "request-2",
            },
            mock_session.user,
            mock_session,
        )
        terminal_question_route = await _post(
            monkeypatch,
            "/project/question",
            _question_payload(mock_session, approval_entry, text="no"),
            mock_session.user,
            mock_session,
        )
        assert terminal_approval_route.status_code == 404
        assert terminal_question_route.status_code == 404


async def test_ask_requires_a_step(mock_chainlit_context, mock_session):
    async with mock_chainlit_context:
        with pytest.raises(ApprovalUnsupported):
            await ask(PROMPT)
