import asyncio
import gc
import importlib
import json
import uuid
from copy import deepcopy
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

import chainlit.approval as approval_module
from chainlit.approval import (
    Approval,
    ApprovalAuthRequired,
    ApprovalEditNotAllowed,
    ApprovalInvalidArguments,
    ApprovalLimitReached,
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalNotReady,
    ApprovalPending,
    ApprovalPersistenceError,
    ApprovalSessionMismatch,
    ApprovalThreadMismatch,
    ApprovalUnsupported,
    guard_pending_approval,
    is_thread_deleted,
    mark_thread_deleted,
    orphan_session_approvals,
    orphan_thread_approvals,
    supersede_thread_approvals,
    transition,
)
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.context import ChainlitContext, context_var
from chainlit.message import Message
from chainlit.server import app
from chainlit.session import HTTPSession, WebsocketSession
from chainlit.step import Step, step as step_decorator
from chainlit.types import ApprovalDecisionRequest
from chainlit.user import User


class _EditArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: int
    note: str = ""
    apiKey: str = ""


class _SecretArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apiKey: SecretStr


class FakeDataLayer:
    """In-memory layer with the same fail-closed write contract."""

    def __init__(self):
        self.steps: Dict[str, Dict[str, Any]] = {}
        self.write_calls = 0
        self.fail_writes = 0
        self.create_delay = 0.0
        self.update_delay = 0.0
        self.write_delay = 0.0
        self.threads: Dict[str, Dict[str, Any]] = {}

    async def create_step(self, step_dict: Dict[str, Any]):
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        self.steps[step_dict["id"]] = deepcopy(step_dict)

    async def update_step(self, step_dict: Dict[str, Any]):
        if self.update_delay:
            await asyncio.sleep(self.update_delay)
        self.steps[step_dict["id"]] = deepcopy(step_dict)

    async def get_thread_author(self, thread_id: str) -> str:
        return "test_user_identifier"

    async def get_thread(self, thread_id: str):
        return deepcopy(self.threads.get(thread_id))

    async def delete_thread(self, thread_id: str):
        for step_id, step in list(self.steps.items()):
            if step.get("threadId") == thread_id:
                del self.steps[step_id]

    async def delete_all_threads(self, user_id: str) -> int:
        thread_ids = {step.get("threadId") for step in self.steps.values()}
        self.steps.clear()
        return len(thread_ids)

    async def write_approval(
        self,
        *,
        thread_id: str,
        step_id: str,
        record: Dict[str, Any],
        expected_revision: Optional[int],
    ) -> bool:
        self.write_calls += 1
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
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
    approval_module._deleted_threads.clear()
    approval_module._managed_thread_users.clear()
    yield
    approval_module._pending_by_approval.clear()
    approval_module._pending_by_step.clear()
    approval_module._step_locks.clear()
    approval_module._terminal_records.clear()
    approval_module._deleted_threads.clear()
    approval_module._managed_thread_users.clear()


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
    assert "approval" not in step.metadata


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


async def test_rejection_never_authorises_execution(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="dangerous_tool", type="tool")
        await step.send()
        executed = []

        async def run_tool():
            decision = await Approval(step=step, arguments={"x": 1}).ask()
            if decision.approved:
                executed.append(True)
            return decision

        task = asyncio.create_task(run_tool())
        entry = await _entry_for(step)
        record = await _decide(mock_session, entry, "rejected", reason="not now")
        decision = await task

    assert record["status"] == "rejected"
    assert decision.decision == "reject"
    assert decision.approved is False
    assert executed == []


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
        ApprovalDecisionRequest(**{**base, "decision": "unknown"})
    with pytest.raises(ValidationError):
        ApprovalDecisionRequest(**{**base, "decisionRequestId": "bad id!"})
    edit = ApprovalDecisionRequest(
        **{**base, "decision": "edit", "editedArguments": {"amount": 2}}
    )
    assert edit.editedArguments == {"amount": 2}


async def _route(
    monkeypatch, payload, user, session, headers=None, base_url="http://testserver"
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

        # Same host, downgraded scheme is not same-origin.
        response = await _route(
            monkeypatch,
            payload,
            mock_session.user,
            mock_session,
            {"Origin": "http://testserver"},
            base_url="https://testserver",
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


async def _delete_route(user, *, path="/project/thread", payload=None):
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.request("DELETE", path, json=payload or {})
    finally:
        app.dependency_overrides.pop(get_current_user, None)


async def test_delete_thread_supersedes_and_never_resurrects(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: layer)
    monkeypatch.setattr("chainlit.data.acl.get_data_layer", lambda: layer)

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, _ = await _pending_ask(step)

        response = await _delete_route(
            mock_session.user,
            payload={"threadId": step.thread_id},
        )
        assert response.status_code == 200
        decision = await task
        assert decision.decision == "superseded"
        assert is_thread_deleted(step.thread_id)
        assert layer.steps == {}

        await step.__aexit__(None, None, None)
        assert step.id not in layer.steps


async def test_delete_all_threads_after_decision_never_resurrects(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: layer)

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)
        await _decide(mock_session, entry)
        decision = await task
        assert decision.approved is True

        response = await _delete_route(mock_session.user, path="/project/threads")
        assert response.status_code == 200
        assert layer.steps == {}

        await step.__aexit__(None, None, None)
        assert step.id not in layer.steps


async def test_guard_refuses_a_thread_until_the_approval_is_terminal(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        with pytest.raises(ApprovalPending):
            guard_pending_approval(step.thread_id)
        assert guard_pending_approval("another-thread") is None

        await _decide(mock_session, entry)
        await task
        guard_pending_approval(step.thread_id)


async def test_session_delete_orphans_its_waiters(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        await orphan_session_approvals("another-generation")
        assert entry.approval_id in approval_module._pending_by_approval

        await orphan_session_approvals("gen-1")
        decision = await task

    assert decision.decision == "orphaned"
    assert decision.approved is False
    assert entry.approval_id not in approval_module._pending_by_approval
    stored = layer.steps[step.id]["metadata"]["approval"]
    assert stored["status"] == "orphaned"
    assert stored["terminalReason"] == "session_ended"


async def test_resume_orphans_a_persisted_pending_record(layer):
    """A reloaded page has no waiter, so the old record becomes orphaned."""
    step = Step(name="tool", type="tool", thread_id="thread-reload")
    layer.steps[step.id] = step.to_dict()
    pending = {
        "version": 1,
        "revision": 1,
        "id": "approval-reload",
        "stepId": step.id,
        "threadId": "thread-reload",
        "tool": "tool",
        "status": "pending",
        "requestedBy": "test_user_identifier",
        "requestedAt": "2026-01-01T00:00:00Z",
        "expiresAt": "2026-01-01T00:05:00Z",
        "argumentsPreview": {"amount": 1},
    }
    layer.steps[step.id]["metadata"] = {"approval": pending}
    thread = {
        "id": "thread-reload",
        "userIdentifier": "test_user_identifier",
        "metadata": {},
        "steps": [deepcopy(layer.steps[step.id])],
    }

    await orphan_thread_approvals(thread)

    assert thread["steps"][0]["metadata"]["approval"]["status"] == "orphaned"
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "orphaned"
    assert (
        layer.steps[step.id]["metadata"]["approval"]["terminalReason"]
        == "server_restart"
    )


async def test_on_message_wrapper_refuses_a_pending_thread(
    layer, mock_chainlit_context, mock_session
):
    from chainlit import callbacks

    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    called = []

    async def handler(message):
        called.append(message.content)

    previous = config.code.on_message
    callbacks.on_message(handler)
    try:
        async with mock_chainlit_context as ctx:
            ctx.emitter.send_approval = AsyncMock()
            step = Step(name="tool", type="tool")
            await step.send()
            task, _ = await _pending_ask(step)

            with pytest.raises(ApprovalPending):
                await config.code.on_message(Message(content="run again"))

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        config.code.on_message = previous

    assert called == []


async def test_on_message_wrapper_binds_and_clears_current_task(
    layer, mock_chainlit_context, mock_session
):
    from chainlit import callbacks

    seen = {}

    async def handler(message):
        seen["is_current"] = mock_session.current_task is asyncio.current_task()

    previous = config.code.on_message
    callbacks.on_message(handler)
    try:
        async with mock_chainlit_context:
            await config.code.on_message(Message(content="hello"))
    finally:
        config.code.on_message = previous

    assert seen["is_current"] is True
    assert mock_session.current_task is None


async def test_step_lock_is_not_retained_after_a_terminal(
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

    gc.collect()
    assert (step.thread_id, step.id) not in approval_module._step_locks


async def test_cancel_during_terminal_write_keeps_pending_and_never_executes(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        layer.write_delay = 1.0
        decision_task = asyncio.create_task(_decide(mock_session, entry))
        await asyncio.sleep(0.05)
        decision_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await decision_task

        assert step.metadata["approval"]["status"] == "pending"
        assert entry.approval_id in approval_module._pending_by_approval
        assert not entry.future.done()
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"

        layer.write_delay = 0
        await _decide(mock_session, entry)
        decision = await task

    assert decision.approved is True


async def test_transition_after_delete_does_not_write_or_execute(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        mark_thread_deleted(step.thread_id)
        with pytest.raises(ApprovalNotPending):
            await _decide(mock_session, entry)

        assert not entry.future.done()
        assert entry.approval_id in approval_module._pending_by_approval
        assert step.metadata["approval"]["status"] == "pending"
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"

        approval_module._deleted_threads.clear()
        await _decide(mock_session, entry)
        assert (await task).approved is True


async def test_delete_suppresses_a_normal_run_step(layer, mock_chainlit_context):
    async with mock_chainlit_context:
        mark_thread_deleted("deleted-thread")
        step = Step(name="on_message", type="run", thread_id="deleted-thread")
        await step.send()
        await step.update()

    assert step.id not in layer.steps


async def test_stop_sweep_racing_a_decision_leaves_one_terminal(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    executed = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        layer.write_delay = 0.2
        decision_task = asyncio.create_task(_decide(mock_session, entry))
        await asyncio.sleep(0.05)

        # The decision holds the step lock, so the stop sweep waits and then
        # sees a terminal record instead of overwriting it.
        await supersede_thread_approvals(step.thread_id, reason="stopped")
        record = await decision_task
        decision = await task

    if decision.approved:
        executed.append(True)
    assert record["status"] == "approved"
    assert decision.decision == "approve"
    assert executed == [True]
    assert layer.steps[step.id]["metadata"]["approval"]["status"] == "approved"
    assert layer.steps[step.id]["metadata"]["approval"]["revision"] == 2


async def test_bulk_delete_supersedes_a_pending_approval(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: layer)

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, _ = await _pending_ask(step)

        response = await _delete_route(mock_session.user, path="/project/threads")
        assert response.status_code == 200
        decision = await task
        assert decision.decision == "superseded"
        assert is_thread_deleted(step.thread_id)
        assert layer.steps == {}

        await step.__aexit__(None, None, None)
        assert step.id not in layer.steps


async def test_edit_is_offered_only_with_a_schema(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(step)

        record = layer.steps[step.id]["metadata"]["approval"]
        assert record["editAllowed"] is False

        with pytest.raises(ApprovalEditNotAllowed):
            await _decide(mock_session, entry, "edited", edited_arguments={"amount": 2})
        assert not task.done()
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"

        await _decide(mock_session, entry, "rejected")
        await task


async def test_edit_schema_must_be_a_forbidding_basemodel(layer, mock_chainlit_context):
    async with mock_chainlit_context:
        step = Step(name="tool", type="tool")
        with pytest.raises(ValueError, match="BaseModel"):
            Approval(step=step, arguments={}, arguments_schema=dict)

        class Loose(BaseModel):
            amount: int

        with pytest.raises(ValueError, match="extra='forbid'"):
            Approval(step=step, arguments={}, arguments_schema=Loose)


async def test_edit_validates_and_returns_the_final_arguments(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    sent = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock(side_effect=lambda p: sent.append(p))
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step,
            {"amount": 1, "apiKey": "sk-original"},
            arguments_schema=_EditArgs,
        )

        record = layer.steps[step.id]["metadata"]["approval"]
        assert record["editAllowed"] is True
        assert record["argumentsPreview"]["apiKey"] == "[redacted]"
        assert "sk-original" not in json.dumps(record)

        stored = await _decide(
            mock_session, entry, "edited", edited_arguments={"amount": 7}
        )
        decision = await task

    assert stored["status"] == "edited"
    assert stored["decision"] == "edit"
    assert stored["editedArgumentsPreview"] == {
        "amount": 7,
        "note": "",
        "apiKey": "[redacted]",
    }
    assert stored["editedArgumentsDigest"].startswith("hmac-sha256:")
    assert stored["decidedBy"] == mock_session.user.identifier

    assert decision.approved is True
    assert decision.decision == "edit"
    assert decision.arguments == {"amount": 7, "note": "", "apiKey": ""}

    # M11: the step trace carries the final, redacted arguments.
    assert decision.step_id == step.id
    assert json.loads(step.input) == {
        "approval": {
            "tool": "tool",
            "argumentsPreview": {
                "amount": 7,
                "note": "",
                "apiKey": "[redacted]",
            },
            "edited": True,
            "redacted": True,
        }
    }
    persisted_input = json.loads(layer.steps[step.id]["input"])
    assert persisted_input["approval"]["argumentsPreview"] == {
        "amount": 7,
        "note": "",
        "apiKey": "[redacted]",
    }
    assert "sk-original" not in json.dumps(layer.steps[step.id])

    # The terminal card event carries the final args and the diff previews.
    terminal = sent[-1]
    assert terminal["status"] == "edited"
    assert terminal["arguments"] == {"amount": 7, "note": "", "apiKey": ""}
    assert terminal["editedArgumentsPreview"] == {
        "amount": 7,
        "note": "",
        "apiKey": "[redacted]",
    }
    assert terminal["editAllowed"] is True


@pytest.mark.parametrize(
    ("edited", "field", "error_type"),
    [
        ({"amount": "super-secret-value"}, "editedArguments.amount", "int_parsing"),
        (
            {"amount": 1, "extra": "super-secret-value"},
            "editedArguments.extra",
            "extra_forbidden",
        ),
        ({"note": "super-secret-value"}, "editedArguments.amount", "missing"),
    ],
)
async def test_invalid_edit_reports_field_and_type_without_the_value(
    layer, mock_chainlit_context, mock_session, edited, field, error_type
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step, {"amount": 1}, arguments_schema=_EditArgs
        )
        writes = layer.write_calls

        with pytest.raises(ApprovalInvalidArguments) as exc:
            await _decide(mock_session, entry, "edited", edited_arguments=edited)

        assert exc.value.field == field
        assert exc.value.error_type == error_type
        assert "super-secret-value" not in str(exc.value)
        assert "super-secret-value" not in repr(exc.value)
        assert layer.write_calls == writes
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"

        await _decide(mock_session, entry, "rejected")
        await task


async def test_edit_requires_edited_arguments(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step, {"amount": 1}, arguments_schema=_EditArgs
        )

        with pytest.raises(ApprovalInvalidArguments) as exc:
            await _decide(mock_session, entry, "edited")
        assert exc.value.field == "editedArguments"
        assert exc.value.error_type == "missing"

        await _decide(mock_session, entry, "rejected")
        await task


async def test_non_json_arguments_are_rejected_before_any_write(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        with pytest.raises(ApprovalInvalidArguments):
            await Approval(
                step=Step(name="tool", type="tool"),
                arguments={"amount": {1, 2}},
            ).ask()

    assert layer.write_calls == 0
    assert not approval_module._pending_by_approval


def test_canonical_digest_is_stable_across_key_orders():
    first = approval_module._arguments_digest(
        {"b": 2, "a": {"y": 1, "x": [3, 4]}}, "salt"
    )
    second = approval_module._arguments_digest(
        {"a": {"x": [3, 4], "y": 1}, "b": 2}, "salt"
    )
    assert first == second
    assert first.startswith("hmac-sha256:")


def test_preview_redaction_is_recursive_case_varied_and_capped():
    deep = value = {}
    for _ in range(12):
        value["child"] = {}
        value = value["child"]
    value["leaf"] = "hidden"

    preview = approval_module._arguments_preview(
        {
            "Authorization": "Bearer top-secret",
            "apiKey": "sk-live",
            "nested": {"client_secret": "shh", "keep": 1},
            "list": [{"X-Api-Key": "k"}, {"keep": 2}],
            "deep": deep,
        }
    )
    assert preview["Authorization"] == "[redacted]"
    assert preview["apiKey"] == "[redacted]"
    assert preview["nested"] == {"client_secret": "[redacted]", "keep": 1}
    assert preview["list"] == [{"X-Api-Key": "[redacted]"}, {"keep": 2}]
    assert "[truncated]" in json.dumps(preview)
    assert "top-secret" not in json.dumps(preview)
    assert "sk-live" not in json.dumps(preview)
    assert "shh" not in json.dumps(preview)

    oversized = approval_module._arguments_preview({"blob": "x" * (17 * 1024)})
    assert oversized == {"truncated": True}


async def test_extra_redaction_keys_apply_to_the_preview(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step,
            {"accountNumber": "1234", "license-key": "abc", "amount": 1},
            redact=("accountNumber", "license-key"),
        )

        preview = layer.steps[step.id]["metadata"]["approval"]["argumentsPreview"]
        assert preview == {
            "accountNumber": "[redacted]",
            "license-key": "[redacted]",
            "amount": 1,
        }

        await _decide(mock_session, entry, "rejected")
        await task


async def test_secretstr_edit_reaches_the_tool_and_digest_unmasked(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step, {"apiKey": "sk-original"}, arguments_schema=_SecretArgs
        )

        stored = await _decide(
            mock_session, entry, "edited", edited_arguments={"apiKey": "sk-edited"}
        )
        decision = await task

    assert decision.arguments == {"apiKey": "sk-edited"}
    assert stored["editedArgumentsPreview"] == {"apiKey": "[redacted]"}
    assert stored["editedArgumentsDigest"] == approval_module._arguments_digest(
        {"apiKey": "sk-edited"}, stored["digestSalt"]
    )
    assert "**********" not in json.dumps(stored)
    assert "sk-edited" not in json.dumps(stored)


async def test_route_edit_succeeds_with_final_arguments_and_diff(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step, {"amount": 1, "apiKey": "sk-original"}, arguments_schema=_EditArgs
        )

        response = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "edit",
                "editedArguments": {"amount": 9, "note": "n", "apiKey": "sk-edited"},
                "decisionRequestId": "request-edit",
            },
            mock_session.user,
            mock_session,
        )
        decision = await task

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "edited"
    assert body["decision"] == "edit"
    assert "arguments" not in body

    assert decision.decision == "edit"
    assert decision.arguments == {"amount": 9, "note": "n", "apiKey": "sk-edited"}

    record = layer.steps[step.id]["metadata"]["approval"]
    assert record["editedArgumentsPreview"] == {
        "amount": 9,
        "note": "n",
        "apiKey": "[redacted]",
    }
    assert "sk-edited" not in json.dumps(record)


async def test_route_edit_without_a_schema_is_422_edit_not_allowed(
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
                "decision": "edit",
                "editedArguments": {"amount": 2},
                "decisionRequestId": "request-edit",
            },
            mock_session.user,
            mock_session,
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "ApprovalEditNotAllowed"
        assert layer.steps[step.id]["metadata"]["approval"]["status"] == "pending"

        await _decide(mock_session, entry, "rejected")
        await task


async def test_route_edit_errors_never_echo_the_value(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()
        step = Step(name="tool", type="tool")
        await step.send()
        task, entry = await _pending_ask(
            step, {"amount": 1}, arguments_schema=_EditArgs
        )

        invalid = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "edit",
                "editedArguments": {"amount": "super-secret-value"},
                "decisionRequestId": "request-edit",
            },
            mock_session.user,
            mock_session,
        )
        assert invalid.status_code == 422
        detail = invalid.json()["detail"]
        assert detail["error"] == "ApprovalInvalidArguments"
        assert detail["field"] == "editedArguments.amount"
        assert detail["type"] == "int_parsing"
        assert "super-secret-value" not in invalid.text

        missing = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "edit",
                "decisionRequestId": "request-edit-2",
            },
            mock_session.user,
            mock_session,
        )
        assert missing.status_code == 422
        assert missing.json()["detail"]["type"] == "missing"

        extra = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "approve",
                "editedArguments": {"amount": 2},
                "decisionRequestId": "request-approve",
            },
            mock_session.user,
            mock_session,
        )
        assert extra.status_code == 422
        assert extra.json()["detail"]["field"] == "editedArguments"
        assert extra.json()["detail"]["type"] == "not_allowed"

        not_object = await _route(
            monkeypatch,
            {
                "sessionId": mock_session.id,
                "approvalId": entry.approval_id,
                "decision": "edit",
                "editedArguments": "super-secret-value",
                "decisionRequestId": "request-edit-3",
            },
            mock_session.user,
            mock_session,
        )
        assert not_object.status_code == 422
        assert not_object.json()["detail"]["error"] == "ApprovalInvalidArguments"
        assert not_object.json()["detail"]["type"] == "object_type"
        assert "super-secret-value" not in not_object.text

        await _decide(mock_session, entry, "rejected")
        await task


async def _next_entry():
    for _ in range(200):
        if approval_module._pending_by_approval:
            return next(iter(approval_module._pending_by_approval.values()))
        await asyncio.sleep(0.01)
    raise AssertionError("approval entry was not registered")


async def test_decorator_persists_only_redacted_preview_and_runs_on_approve(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True)
        async def send_email(amount: int, apiKey: str):
            calls.append((amount, apiKey))
            return "sent"

        task = asyncio.create_task(send_email(amount=5, apiKey="sk-super-secret"))
        entry = await _next_entry()
        row = layer.steps[entry.step_id]

        assert "sk-super-secret" not in json.dumps(row)
        assert json.loads(row["input"]) == {
            "approval": {
                "tool": "send_email",
                "argumentsPreview": {"amount": 5, "apiKey": "[redacted]"},
                "redacted": True,
            }
        }
        assert row["metadata"]["approval"]["argumentsPreview"] == {
            "amount": 5,
            "apiKey": "[redacted]",
        }

        await _decide(mock_session, entry)
        result = await task

    assert result == "sent"
    assert calls == [(5, "sk-super-secret")]
    assert entry.step.is_error is False
    assert layer.steps[entry.step_id]["metadata"]["approval"]["status"] == "approved"


async def test_decorator_returns_none_without_running_on_reject(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True)
        async def tool(amount: int):
            calls.append(amount)
            return "ran"

        task = asyncio.create_task(tool(amount=1))
        entry = await _next_entry()
        await _decide(mock_session, entry, "rejected", reason="not now")
        result = await task

    assert result is None
    assert calls == []
    assert entry.step.is_error is True
    assert entry.step.output == "Approval rejected"
    assert layer.steps[entry.step_id]["metadata"]["approval"]["status"] == "rejected"


async def test_decorator_returns_none_without_running_on_expiry(
    layer, mock_chainlit_context, mock_session, monkeypatch
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    monkeypatch.setattr(config.project, "session_timeout", 0.05)
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True)
        async def tool(amount: int):
            calls.append(amount)
            return "ran"

        task = asyncio.create_task(tool(amount=1))
        entry = await _next_entry()
        result = await asyncio.wait_for(task, 2)

    assert result is None
    assert calls == []
    assert entry.step.is_error is True
    assert entry.step.output == "Approval expired"
    assert layer.steps[entry.step_id]["metadata"]["approval"]["status"] == "expired"


async def test_decorator_returns_none_without_running_on_supersede(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True)
        async def tool(amount: int):
            calls.append(amount)
            return "ran"

        task = asyncio.create_task(tool(amount=1))
        entry = await _next_entry()
        await transition(
            entry.approval_id,
            "superseded",
            thread_id=mock_session.thread_id,
            session_generation=mock_session.generation,
            reason="replaced",
        )
        result = await task

    assert result is None
    assert calls == []
    assert entry.step.output == "Approval superseded"
    assert layer.steps[entry.step_id]["metadata"]["approval"]["status"] == "superseded"


async def test_decorator_does_not_run_when_request_write_fails(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []
    layer.fail_writes = 1

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True)
        async def tool(amount: int):
            calls.append(amount)
            return "ran"

        task = asyncio.create_task(tool(amount=1))
        with pytest.raises(ApprovalPersistenceError):
            await task

    assert calls == []
    assert not approval_module._pending_by_approval
    assert all(
        "approval" not in row.get("metadata", {}) for row in layer.steps.values()
    )


async def test_decorator_edit_runs_with_validated_arguments_only(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True, approval_schema=_EditArgs)
        async def tool(amount: int, note: str = "", apiKey: str = ""):
            calls.append((amount, note, apiKey))
            return "ran"

        task = asyncio.create_task(tool(amount=1, apiKey="sk-original"))
        entry = await _next_entry()
        await _decide(mock_session, entry, "edited", edited_arguments={"amount": 7})
        result = await task

    assert result == "ran"
    assert calls == [(7, "", "")]
    assert entry.step.output == "ran"
    row = layer.steps[entry.step_id]
    assert "sk-original" not in json.dumps(row)
    assert json.loads(row["input"]) == {
        "approval": {
            "tool": "tool",
            "argumentsPreview": {
                "amount": 7,
                "note": "",
                "apiKey": "[redacted]",
            },
            "edited": True,
            "redacted": True,
        }
    }


async def test_decorator_invalid_edit_does_not_run(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        @step_decorator(type="tool", requires_approval=True, approval_schema=_EditArgs)
        async def tool(amount: int, note: str = "", apiKey: str = ""):
            calls.append(amount)
            return "ran"

        task = asyncio.create_task(tool(amount=1))
        entry = await _next_entry()
        with pytest.raises(ApprovalInvalidArguments):
            await _decide(
                mock_session,
                entry,
                "edited",
                edited_arguments={"amount": "super-secret-value"},
            )
        assert not task.done()
        assert calls == []

        await _decide(mock_session, entry, "rejected")
        assert await task is None
    assert calls == []


async def test_approval_route_never_calls_action_callbacks(
    layer, monkeypatch, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    mock_session.unavailable_chat_profile = None
    called = []

    async def action_callback(action):
        called.append(action)

    monkeypatch.setattr(
        config.code, "action_callbacks", {"fake_action": action_callback}
    )

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
        )
        await task

    assert response.status_code == 200
    assert called == []


@pytest.mark.parametrize("metadata_as_json", [False, True], ids=["dict", "string"])
async def test_resume_normalizes_string_and_dict_step_metadata(
    monkeypatch, metadata_as_json
):
    approval_id = str(uuid.uuid4())
    pending = {
        "version": 1,
        "revision": 1,
        "id": approval_id,
        "stepId": "step-1",
        "threadId": "thread-1",
        "tool": "tool",
        "title": "<b>title</b>",
        "status": "pending",
        "requestedBy": "test_user_identifier",
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
    metadata = {"approval": pending, "keep": "me"}
    if metadata_as_json:
        metadata = json.dumps(metadata)
    thread = {
        "id": "thread-1",
        "metadata": {},
        "steps": [{"id": "step-1", "metadata": metadata}],
    }
    written = []

    class Layer:
        async def write_approval(
            self, *, thread_id, step_id, record, expected_revision
        ):
            written.append((thread_id, step_id, record, expected_revision))
            return True

    monkeypatch.setattr(approval_module, "get_data_layer", lambda: Layer())

    await orphan_thread_approvals(thread)

    stored = thread["steps"][0]["metadata"]
    assert isinstance(stored, dict)
    assert stored["keep"] == "me"
    assert stored["approval"]["status"] == "orphaned"
    assert stored["approval"]["title"] == "<b>title</b>"
    assert written
    assert written[0][2]["status"] == "orphaned"


async def test_decorator_edit_on_a_method_keeps_self_out_of_the_record(
    layer, mock_chainlit_context, mock_session
):
    mock_session.generation = "gen-1"
    mock_session.has_first_interaction = True
    calls = []

    async with mock_chainlit_context as ctx:
        ctx.emitter.send_approval = AsyncMock()

        class Tool:
            @step_decorator(
                type="tool", requires_approval=True, approval_schema=_EditArgs
            )
            async def run(self, amount: int, note: str = "", apiKey: str = ""):
                assert self is tool
                calls.append(amount)
                return "ran"

        tool = Tool()
        task = asyncio.create_task(tool.run(amount=1, apiKey="sk-original"))
        entry = await _next_entry()
        record = layer.steps[entry.step_id]["metadata"]["approval"]
        assert record["argumentsPreview"] == {
            "amount": 1,
            "note": "",
            "apiKey": "[redacted]",
        }

        await _decide(mock_session, entry, "edited", edited_arguments={"amount": 7})
        assert await task == "ran"

    assert calls == [7]
