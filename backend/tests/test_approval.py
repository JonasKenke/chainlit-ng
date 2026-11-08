import asyncio
import importlib
import json
import uuid
from copy import deepcopy
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

import chainlit.approval as approval_module
from chainlit.approval import (
    Approval,
    ApprovalAuthRequired,
    ApprovalLimitReached,
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalNotReady,
    ApprovalPersistenceError,
    ApprovalSessionMismatch,
    ApprovalThreadMismatch,
    ApprovalUnsupported,
    transition,
)
from chainlit.auth import get_current_user
from chainlit.context import ChainlitContext, context_var
from chainlit.server import app
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.step import Step
from chainlit.types import ApprovalDecisionRequest
from chainlit.user import User


class FakeDataLayer:
    """In-memory layer with the same fail-closed write contract."""

    def __init__(self):
        self.steps: Dict[str, Dict[str, Any]] = {}
        self.write_calls = 0
        self.fail_writes = 0
        self.create_delay = 0.0
        self.update_delay = 0.0

    async def create_step(self, step_dict: Dict[str, Any]):
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        self.steps[step_dict["id"]] = deepcopy(step_dict)

    async def update_step(self, step_dict: Dict[str, Any]):
        if self.update_delay:
            await asyncio.sleep(self.update_delay)
        self.steps[step_dict["id"]] = deepcopy(step_dict)

    async def write_approval(
        self,
        *,
        thread_id: str,
        step_id: str,
        record: Dict[str, Any],
        expected_revision: Optional[int],
    ) -> bool:
        self.write_calls += 1
        if self.fail_writes:
            self.fail_writes -= 1
            return False
        step = self.steps.get(step_id)
        if step is None:
            return False
        metadata = step.setdefault("metadata", {})
        existing = metadata.get("approval")
        if expected_revision is None:
            if isinstance(existing, dict) and (
                existing.get("status") == "pending"
                and existing.get("id") != record.get("id")
            ):
                return False
        elif (
            not isinstance(existing, dict)
            or existing.get("id") != record.get("id")
            or existing.get("revision") != expected_revision
        ):
            return False
        metadata["approval"] = deepcopy(record)
        return True


@pytest.fixture(autouse=True)
def clean_registry():
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()
    yield
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()


@pytest.fixture
def layer(monkeypatch):
    fake = FakeDataLayer()
    monkeypatch.setattr("chainlit.approval.get_data_layer", lambda: fake)
    step_module = importlib.import_module("chainlit.step")
    monkeypatch.setattr(step_module, "get_data_layer", lambda: fake)
    return fake


async def _entry_for(step: Step):
    for _ in range(100):
        approval_id = approval_module._pending_by_step.get((step.thread_id, step.id))
        if approval_id:
            return approval_module._pending_by_approval[approval_id]
        await asyncio.sleep(0.01)
    raise AssertionError("approval entry was not registered")


async def _pending_ask(step: Step, arguments=None, timeout=30):
    before = set(approval_module._pending_by_approval)
    task = asyncio.create_task(
        Approval(step=step, arguments=arguments or {"amount": 1}, timeout=timeout).ask()
    )
    for _ in range(100):
        new_ids = set(approval_module._pending_by_approval) - before
        if new_ids:
            return task, approval_module._pending_by_approval[new_ids.pop()]
        await asyncio.sleep(0.01)
    raise AssertionError("approval entry was not registered")


def _user(identifier="test_user_identifier"):
    return User(identifier=identifier)


async def _decide(session, entry, decision="approved", **kwargs):
    return await transition(
        entry.approval_id,
        decision,
        actor=session.user,
        thread_id=session.thread_id,
        session_generation=session.generation,
        **kwargs,
    )


async def test_request_write_failure_raises_and_never_executes(
    layer, mock_chainlit_context, mock_session
):
    executed = []
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    layer.fail_writes = 1

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="dangerous_tool", type="tool")
        await step.send()

        async def run_tool():
            decision = await Approval(step=step, arguments={"x": 1}).ask()
            if decision.approved:
                executed.append(True)

        with pytest.raises(ApprovalPersistenceError):
            await run_tool()

    assert executed == []
    assert not approval_module._pending_by_approval
    assert "approval" not in layer.steps[step.id]["metadata"]


async def test_decision_write_failure_keeps_pending_and_retry_succeeds(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        executed = []

        async def run_tool():
            decision = await Approval(
                step=step, arguments={"amount": 1}, timeout=30
            ).ask()
            if decision.approved:
                executed.append(True)

        task = asyncio.create_task(run_tool())
        entry = await _entry_for(step)

        layer.fail_writes = 1
        with pytest.raises(ApprovalPersistenceError):
            await _decide(mock_session, entry)

        assert not task.done()
        assert executed == []
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"
        assert entry.approval_id in approval_module._pending_by_approval

        record = await _decide(mock_session, entry)
        await task

    assert record["status"] == "approved"
    assert executed == [True]
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "approved"


async def test_timeout_uses_the_same_transition_and_executes_nothing(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        decision = await Approval(step=step, arguments={"x": 1}, timeout=0.02).ask()

    assert decision.decision == "expired"
    assert decision.approved is False
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "expired"
    assert (step.thread_id, step.id) not in approval_module._pending_by_step


async def test_second_transition_returns_stored_record_and_changes_nothing(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)
        await _decide(mock_session, entry)
        await task
        writes = layer.write_calls

        with pytest.raises(ApprovalNotPending) as exc:
            await _decide(mock_session, entry, "rejected")

    assert exc.value.record["status"] == "approved"
    assert layer.write_calls == writes


async def test_second_ask_on_same_step_supersedes_the_first(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        first_task, first_entry = await _pending_ask(step, {"x": 1})

        second_task, second_entry = await _pending_ask(step, {"x": 2})
        first_decision = await first_task
        assert first_decision.decision == "superseded"
        assert second_entry.approval_id != first_entry.approval_id

        record = await _decide(mock_session, second_entry)
        second_decision = await second_task

    assert record["status"] == "approved"
    assert second_decision.approved is True
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "approved"
    assert (
        layer.steps[step.id]["metadata"]["approval"]["id"] == second_entry.approval_id
    )


async def test_wrong_user_cannot_transition_a_pending_approval(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        with pytest.raises(ApprovalNotFound):
            await transition(
                entry.approval_id,
                "approved",
                actor=_user("someone-else"),
                thread_id=mock_session.thread_id,
                session_generation=mock_session.generation,
            )

        await _decide(mock_session, entry, "rejected")
        await task


async def test_new_session_with_same_client_id_cannot_decide(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        new_session = Mock(spec=WebsocketSession)
        new_session.id = mock_session.id
        new_session.thread_id = mock_session.thread_id
        new_session.user = mock_session.user
        new_session.generation = "gen-2"

        with pytest.raises(ApprovalSessionMismatch):
            await _decide(new_session, entry)

        await _decide(mock_session, entry, "rejected")
        await task


async def test_registry_is_keyed_by_thread_and_step(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        assert (
            approval_module._pending_by_step[(step.thread_id, step.id)]
            == entry.approval_id
        )

        await _decide(mock_session, entry, "rejected")
        await task


async def test_wrong_thread_is_not_found(layer, mock_chainlit_context, mock_session):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        with pytest.raises(ApprovalNotFound):
            await transition(
                entry.approval_id,
                "approved",
                actor=mock_session.user,
                thread_id="another-thread",
                session_generation=mock_session.generation,
            )

        await _decide(mock_session, entry, "rejected")
        await task


async def test_unknown_approval_is_not_found(layer):
    with pytest.raises(ApprovalNotFound):
        await transition("missing", "approved", actor=_user())


async def test_auth_disabled_is_refused_before_any_write(
    layer, mock_chainlit_context, mock_session
):
    mock_session.user = None
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context:
        with pytest.raises(ApprovalAuthRequired):
            await Approval(step=Step(name="tool", type="tool"), arguments={}).ask()

    assert layer.write_calls == 0


async def test_pre_first_message_is_refused(layer, mock_chainlit_context, mock_session):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = False

    async with mock_chainlit_context:
        with pytest.raises(ApprovalNotReady):
            await Approval(step=Step(name="tool", type="tool"), arguments={}).ask()

    assert layer.write_calls == 0


async def test_http_session_is_unsupported(layer, persisted_test_user):
    http_session = HTTPSession(
        id="http-session",
        client_type="webapp",
        thread_id="thread-1",
        user=persisted_test_user,
        user_env={},
    )
    token = context_var.set(ChainlitContext(http_session))
    try:
        with pytest.raises(ApprovalUnsupported):
            await Approval(step=Step(name="tool", type="tool"), arguments={}).ask()
    finally:
        context_var.reset(token)


async def test_step_thread_mismatch_is_refused(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context:
        step = Step(name="tool", type="tool", thread_id="other-thread")
        with pytest.raises(ApprovalThreadMismatch):
            await Approval(step=step, arguments={}).ask()

    assert layer.write_calls == 0


async def test_expired_decision_returns_terminal_record_and_does_not_execute(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step, timeout=0.01)
        await asyncio.sleep(0.03)

        with pytest.raises(ApprovalNotPending) as exc:
            await _decide(mock_session, entry)

        decision = await task

    assert exc.value.record["status"] == "expired"
    assert decision.decision == "expired"
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "expired"


async def test_per_session_cap_raises(layer, mock_chainlit_context, mock_session):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    tasks = []
    entries = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        for index in range(8):
            step = Step(name=f"tool-{index}", type="tool")
            await step.send()
            task, entry = await _pending_ask(step, timeout=30)
            tasks.append(task)
            entries.append(entry)

        ninth = Step(name="tool-8", type="tool")
        await ninth.send()
        with pytest.raises(ApprovalLimitReached):
            await Approval(step=ninth, arguments={}).ask()

        for entry in entries:
            await transition(
                entry.approval_id,
                "superseded",
                reason="test",
                retry_on_failure=True,
            )
        await asyncio.gather(*tasks)


async def test_no_data_layer_works_in_process(
    monkeypatch, mock_chainlit_context, mock_session
):
    monkeypatch.setattr("chainlit.approval.get_data_layer", lambda: None)
    step_module = importlib.import_module("chainlit.step")
    monkeypatch.setattr(step_module, "get_data_layer", lambda: None)
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        task, entry = await _pending_ask(step)
        record = await _decide(mock_session, entry)
        decision = await task

    assert record["status"] == "approved"
    assert decision.approved is True


async def test_pending_record_has_every_field_and_redacts_secrets(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    arguments = {
        "password": "hunter2",
        "nested": {"Authorization": "Bearer super-secret"},
        "amount": 5,
    }

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step, arguments)

        record = layer.steps[step.id]["metadata"]["approval"]
        assert record["revision"] == 1
        assert record["digestSalt"]
        assert record["argumentsDigest"].startswith("hmac-sha256:")
        assert record["argumentsPreview"] == {
            "password": "[redacted]",
            "nested": {"Authorization": "[redacted]"},
            "amount": 5,
        }
        assert "hunter2" not in json.dumps(record)
        assert "super-secret" not in json.dumps(record)
        expected_fields = {
            "version",
            "revision",
            "id",
            "stepId",
            "threadId",
            "tool",
            "title",
            "status",
            "requestedBy",
            "requestedAt",
            "expiresAt",
            "editAllowed",
            "digestSalt",
            "argumentsDigest",
            "argumentsPreview",
            "decidedBy",
            "decidedAt",
            "decision",
            "editedArgumentsPreview",
            "editedArgumentsDigest",
            "reason",
            "decisionRequestId",
            "terminalReason",
            "terminalAt",
        }
        assert expected_fields <= set(record)

        await _decide(mock_session, entry, "rejected", reason="not now")
        terminal = layer.steps[step.id]["metadata"]["approval"]
        assert expected_fields <= set(terminal)
        assert terminal["revision"] == 2
        assert terminal["reason"] == "not now"
        assert terminal["terminalReason"] is None
        assert terminal["decidedBy"] == mock_session.user.identifier
        await task


async def test_digest_is_salted_hmac(layer, mock_chainlit_context, mock_session):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    digests = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        for _ in range(2):
            step = Step(name="tool", type="tool")
            await step.send()
            task, entry = await _pending_ask(step, {"same": "arguments"})
            digests.append(
                layer.steps[step.id]["metadata"]["approval"]["argumentsDigest"]
            )
            await _decide(mock_session, entry, "rejected")
            await task

    assert digests[0] != digests[1]
    assert all(digest.startswith("hmac-sha256:") for digest in digests)


async def test_slow_create_does_not_lose_pending_record(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    layer.create_delay = 0.05

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"
        await _decide(mock_session, entry)
        decision = await task

    assert decision.approved is True
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "approved"


async def test_slow_update_and_app_exit_cannot_resurrect_pending(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        layer.update_delay = 0.05
        update_task = asyncio.create_task(step.update())
        await asyncio.sleep(0)
        await _decide(mock_session, entry)
        decision = await task
        await update_task

        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "approved"

        await step.__aexit__(None, None, None)
        persisted = layer.steps[step.id]["metadata"]["approval"]
        assert persisted["status"] == "approved"
        assert persisted["revision"] == 2

    assert decision.approved is True


def test_decision_request_rejects_extra_fields():
    base = {
        "sessionId": "session-1",
        "approvalId": str(uuid.uuid4()),
        "decision": "approve",
        "decisionRequestId": "request-1",
    }
    ApprovalDecisionRequest(**base)

    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(**base, arguments={"amount": 10})
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(**{**base, "decision": "edit"})
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(**{**base, "decisionRequestId": "bad id!"})


async def _route(monkeypatch, payload, user, session, headers=None):
    server_module = importlib.import_module("chainlit.server")

    class _SessionLookup:
        @staticmethod
        def get_by_id(session_id):
            return session

    monkeypatch.setattr(server_module, "WebsocketSession", _SessionLookup)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/project/approval",
                json=payload,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_route_approves_pending_approval(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        response = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "approve",
                "decisionRequestId": "request-1",
            },
            mock_session.user,
            mock_session,
            {"Origin": "http://testserver"},
        )
        decision = await task

    assert response.status_code == 200
    body = response.json()
    assert body["approvalId"] == entry.approval_id
    assert body["status"] == "approved"
    assert body["decision"] == "approve"
    assert "arguments" not in body
    assert decision.approved is True


async def test_route_rejects_wrong_user(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        response = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "approve",
                "decisionRequestId": "request-1",
            },
            _user("someone-else"),
            mock_session,
        )
        assert response.status_code == 403
        assert not task.done()

        await _decide(mock_session, entry, "rejected")
        await task


async def test_route_rejects_cross_origin_and_extra_fields(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        payload = {
            "sessionId": mock_session.id,
            "approvalId": entry.approval_id,
            "decision": "approve",
            "decisionRequestId": "request-1",
        }
        response = await _route(
            monkeypatch,
            payload,
            mock_session.user,
            mock_session,
            {"Origin": "http://evil.test"},
        )
        assert response.status_code == 403

        response = await _route(
            monkeypatch,
            {**payload, "arguments": {"amount": 99}},
            mock_session.user,
            mock_session,
        )
        assert response.status_code == 422

        await _decide(mock_session, entry, "rejected")
        await task


async def test_route_terminal_state_returns_minimal_dto(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)
        await _decide(mock_session, entry)
        await task

        response = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "reject",
                "decisionRequestId": "request-2",
            },
            mock_session.user,
            mock_session,
        )

    assert response.status_code == 409
    body = response.json()
    assert body["status"] == "approved"
    assert body["error"] == "ApprovalNotPending"
    assert "arguments" not in body


async def test_route_unknown_approval_is_404(layer, monkeypatch, mock_session):
    mock_session.generation = "gen-1"
    mock_session.user = _user()
    mock_session.unavailable_chat_profile = None

    response = await _route(
        monkeypatch,
        {
            "sessionId": mock_session.id,
            "approvalId": str(uuid.uuid4()),
            "decision": "approve",
            "decisionRequestId": "request-1",
        },
        mock_session.user,
        mock_session,
    )
    assert response.status_code == 404
