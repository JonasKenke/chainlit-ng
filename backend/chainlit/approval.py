"""Author-bound, persisted tool-call approvals.

S1: an approval is bound to a :class:`~chainlit.step.Step`, can only be
decided by the authenticated user who requested it, and is only resolved
after the terminal record was written (proven durable when a data layer is
configured).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, Literal, Mapping, NoReturn, Optional

from chainlit.config import config
from chainlit.context import context
from chainlit.data import get_data_layer
from chainlit.logger import logger
from chainlit.session import WebsocketSession
from chainlit.step import Step
from chainlit.user import User

if TYPE_CHECKING:
    from chainlit.emitter import BaseChainlitEmitter

APPROVAL_METADATA_KEY = "approval"
MAX_PENDING_APPROVALS_PER_SESSION = 8
_TERMINAL_RECORD_LIMIT = 256

ApprovalStatus = Literal[
    "pending", "approved", "rejected", "expired", "superseded", "orphaned"
]
Decision = Literal["approve", "reject", "expired", "superseded", "orphaned"]

_USER_STATUSES = {"approved", "rejected"}
_SYSTEM_STATUSES = {"expired", "superseded", "orphaned"}

_SECRET_KEY_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "credential",
)
_MAX_PREVIEW_DEPTH = 8
_MAX_PREVIEW_CHARS = 16 * 1024


class ApprovalError(Exception):
    """Base error for approval refusals."""

    code = "ApprovalError"
    status_code = 400

    def __init__(
        self, message: Optional[str] = None, *, record: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(message or self.code)
        self.record = record


class ApprovalAuthRequired(ApprovalError):
    code = "ApprovalAuthRequired"
    status_code = 401


class ApprovalNotReady(ApprovalError):
    code = "ApprovalNotReady"
    status_code = 409


class ApprovalUnsupported(ApprovalError):
    code = "ApprovalUnsupported"
    status_code = 400


class ApprovalThreadMismatch(ApprovalError):
    code = "ApprovalThreadMismatch"
    status_code = 403


class ApprovalSessionMismatch(ApprovalError):
    code = "ApprovalSessionMismatch"
    status_code = 403


class ApprovalNotPending(ApprovalError):
    code = "ApprovalNotPending"
    status_code = 409


class ApprovalNotFound(ApprovalError):
    code = "ApprovalNotFound"
    status_code = 404


class ApprovalPersistenceError(ApprovalError):
    code = "ApprovalPersistenceError"
    status_code = 500


class ApprovalInvalidArguments(ApprovalError):
    code = "ApprovalInvalidArguments"
    status_code = 422


class ApprovalLimitReached(ApprovalError):
    code = "ApprovalLimitReached"
    status_code = 429


@dataclass(frozen=True)
class ApprovalDecision:
    approval_id: str
    thread_id: str
    step_id: str
    decision: Decision
    approved: bool
    arguments: Mapping[str, Any]
    user: Optional[User]
    decided_at: str
    comment: Optional[str] = None


@dataclass
class _Entry:
    approval_id: str
    thread_id: str
    step_id: str
    step: Step
    future: asyncio.Future
    emitter: BaseChainlitEmitter
    requested_by: str
    session_generation: str
    revision: int
    expires_at_ts: float
    arguments: Mapping[str, Any] = field(default_factory=dict)


# Module-level, in-process registry. S1 runs in one worker with sticky
# sessions (KRE-188 tracks multi-worker).
_pending_by_approval: Dict[str, _Entry] = {}
_pending_by_step: Dict[tuple[str, str], str] = {}
_step_locks: Dict[tuple[str, str], asyncio.Lock] = {}
_terminal_records: Dict[str, tuple[str, Dict[str, Any]]] = {}

_PROCESS_DIGEST_KEY = secrets.token_bytes(32)


def step_lock(thread_id: str, step_id: str) -> asyncio.Lock:
    """Return the one lock that serializes writes for a step."""
    key = (thread_id, step_id)
    lock = _step_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _step_locks[key] = lock
    return lock


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ApprovalInvalidArguments("arguments must be JSON-compatible") from exc


def _digest_key() -> bytes:
    secret = os.environ.get("CHAINLIT_AUTH_SECRET")
    if not secret:
        return _PROCESS_DIGEST_KEY
    # HKDF-SHA256(CHAINLIT_AUTH_SECRET, info=b"chainlit:approval-digest")
    prk = hmac.new(b"\x00" * 32, secret.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, b"chainlit:approval-digest\x01", hashlib.sha256).digest()


def _arguments_digest(arguments: Mapping[str, Any], salt: str) -> str:
    payload = salt.encode("utf-8") + _canonical_json(arguments).encode("utf-8")
    digest = hmac.new(_digest_key(), payload, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_PREVIEW_DEPTH:
        return "[truncated]"
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower().replace("-", "_")
            if any(part in lower for part in _SECRET_KEY_PARTS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact(item, depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1) for item in value]
    return value


def _arguments_preview(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    preview = _redact(arguments)
    if len(json.dumps(preview, default=str)) > _MAX_PREVIEW_CHARS:
        return {"truncated": True}
    return preview


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _payload(record: Mapping[str, Any], arguments: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": record["id"],
        "stepId": record["stepId"],
        "threadId": record["threadId"],
        "tool": record["tool"],
        "title": record.get("title"),
        "status": record["status"],
        "requestedBy": record.get("requestedBy"),
        "requestedAt": record.get("requestedAt"),
        "expiresAt": record.get("expiresAt"),
        "revision": record.get("revision"),
        "decidedBy": record.get("decidedBy"),
        "decidedAt": record.get("decidedAt"),
        "decision": record.get("decision"),
        "terminalReason": record.get("terminalReason"),
        "arguments": dict(arguments),
    }


async def _emit(
    entry: _Entry, record: Mapping[str, Any], arguments: Mapping[str, Any]
) -> None:
    try:
        await entry.emitter.send_approval(_payload(record, arguments))
    except Exception:
        logger.exception("Failed to emit approval event")


def _remember_terminal(thread_id: str, record: Dict[str, Any]) -> None:
    _terminal_records[record["id"]] = (thread_id, deepcopy(record))
    while len(_terminal_records) > _TERMINAL_RECORD_LIMIT:
        _terminal_records.pop(next(iter(_terminal_records)))


def _raise_missing(approval_id: str, thread_id: Optional[str]) -> NoReturn:
    terminal = _terminal_records.get(approval_id)
    if terminal:
        stored_thread, record = terminal
        if thread_id is not None and thread_id != stored_thread:
            raise ApprovalNotFound("Approval not found")
        raise ApprovalNotPending("Approval is no longer pending", record=record)
    raise ApprovalNotFound("Approval not found")


async def _write(
    entry: _Entry, record: Dict[str, Any], expected_revision: Optional[int]
) -> bool:
    data_layer = get_data_layer()
    if data_layer is None:
        return True
    try:
        return bool(
            await data_layer.write_approval(
                thread_id=entry.thread_id,
                step_id=entry.step_id,
                record=deepcopy(record),
                expected_revision=expected_revision,
            )
        )
    except Exception:
        logger.exception("Approval persistence failed")
        return False


async def _transition_locked(
    entry: _Entry,
    to_status: ApprovalStatus,
    *,
    actor: Optional[User] = None,
    reason: Optional[str] = None,
    decision_request_id: Optional[str] = None,
    retry_on_failure: bool = False,
) -> Dict[str, Any]:
    """Persist one terminal record for a live pending entry."""
    pending = entry.step.metadata.get(APPROVAL_METADATA_KEY)
    if not isinstance(pending, dict) or pending.get("status") != "pending":
        raise ApprovalNotPending(
            "Approval is no longer pending",
            record=deepcopy(pending) if isinstance(pending, dict) else None,
        )

    if to_status in _USER_STATUSES and (
        actor is None or actor.identifier != entry.requested_by
    ):
        raise ApprovalNotFound("Approval not found")

    now = datetime.now(timezone.utc)
    if to_status in _USER_STATUSES and now.timestamp() >= entry.expires_at_ts:
        stored = await _transition_locked(
            entry, "expired", reason="timeout", retry_on_failure=retry_on_failure
        )
        raise ApprovalNotPending("Approval has expired", record=stored)

    record = deepcopy(pending)
    record.update(
        {
            "revision": int(pending.get("revision", entry.revision)) + 1,
            "status": to_status,
            "decidedBy": actor.identifier if actor is not None else None,
            "decidedAt": _iso(now) if actor is not None else None,
            "decision": (
                "approve"
                if to_status == "approved"
                else "reject"
                if to_status == "rejected"
                else None
            ),
            "reason": reason if to_status in _USER_STATUSES else None,
            "terminalReason": (
                None
                if to_status in _USER_STATUSES
                else reason or ("timeout" if to_status == "expired" else None)
            ),
            "terminalAt": _iso(now),
            "decisionRequestId": decision_request_id,
        }
    )

    entry.step.metadata[APPROVAL_METADATA_KEY] = record
    attempts = 3 if retry_on_failure else 1
    for attempt in range(attempts):
        if await _write(entry, record, expected_revision=pending.get("revision")):
            break
        if attempt + 1 == attempts:
            entry.step.metadata[APPROVAL_METADATA_KEY] = pending
            if retry_on_failure:
                logger.error(
                    "Approval %s terminal state could not be persisted",
                    entry.approval_id,
                )
                break
            raise ApprovalPersistenceError("Approval persistence failed")
        await asyncio.sleep(0.05 * (attempt + 1))

    entry.revision = int(record["revision"])
    _pending_by_approval.pop(entry.approval_id, None)
    if _pending_by_step.get((entry.thread_id, entry.step_id)) == entry.approval_id:
        _pending_by_step.pop((entry.thread_id, entry.step_id), None)
    _remember_terminal(entry.thread_id, record)

    if not entry.future.done():
        entry.future.set_result(record)
    await _emit(entry, record, entry.arguments)
    return record


async def transition(
    approval_id: str,
    to_status: ApprovalStatus,
    *,
    actor: Optional[User] = None,
    thread_id: Optional[str] = None,
    session_generation: Optional[str] = None,
    reason: Optional[str] = None,
    decision_request_id: Optional[str] = None,
    retry_on_failure: bool = False,
) -> Dict[str, Any]:
    """The only terminal writer for an approval.

    A second transition returns the stored record and changes nothing.
    """
    if to_status not in _USER_STATUSES and to_status not in _SYSTEM_STATUSES:
        raise ValueError(f"Cannot transition an approval to {to_status}")

    entry = _pending_by_approval.get(approval_id)
    if entry is None:
        _raise_missing(approval_id, thread_id)

    if thread_id is not None and entry.thread_id != thread_id:
        raise ApprovalNotFound("Approval not found")
    if (
        session_generation is not None
        and entry.session_generation != session_generation
    ):
        raise ApprovalSessionMismatch("Approval belongs to another session")

    async with step_lock(entry.thread_id, entry.step_id):
        current = _pending_by_approval.get(approval_id)
        if current is None:
            _raise_missing(approval_id, thread_id)
        if thread_id is not None and current.thread_id != thread_id:
            raise ApprovalNotFound("Approval not found")
        if (
            session_generation is not None
            and current.session_generation != session_generation
        ):
            raise ApprovalSessionMismatch("Approval belongs to another session")
        return await _transition_locked(
            current,
            to_status,
            actor=actor,
            reason=reason,
            decision_request_id=decision_request_id,
            retry_on_failure=retry_on_failure,
        )


def _decision_from_record(
    record: Mapping[str, Any],
    arguments: Mapping[str, Any],
    user: Optional[User],
) -> ApprovalDecision:
    status = record["status"]
    decision: Decision = (
        "approve"
        if status == "approved"
        else "reject"
        if status == "rejected"
        else status
    )
    decided_at = record.get("decidedAt") or record.get("terminalAt") or ""
    return ApprovalDecision(
        approval_id=record["id"],
        thread_id=record["threadId"],
        step_id=record["stepId"],
        decision=decision,
        approved=status == "approved",
        arguments=deepcopy(arguments),
        user=user if status in _USER_STATUSES else None,
        decided_at=decided_at,
        comment=record.get("reason"),
    )


class Approval:
    """A one-shot approval request bound to a Step.

    The server-held ``arguments`` are the only execution source; a decision
    never carries client-supplied arguments.
    """

    def __init__(
        self,
        *,
        step: Step,
        arguments: Mapping[str, Any],
        tool: Optional[str] = None,
        title: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.step = step
        self.arguments = deepcopy(dict(arguments))
        self.tool = tool or step.name
        self.title = title[:200] if title else None
        self.timeout = timeout

    async def ask(self) -> ApprovalDecision:
        session = context.session
        if not isinstance(session, WebsocketSession):
            raise ApprovalUnsupported("Approvals require a websocket session")
        if not session.user or not session.user.identifier:
            raise ApprovalAuthRequired("An authenticated user is required")
        if self.step.thread_id != session.thread_id:
            raise ApprovalThreadMismatch("Approval step belongs to another thread")
        if not session.has_first_interaction:
            raise ApprovalNotReady("Approvals require a user message first")

        _canonical_json(self.arguments)
        generation = getattr(session, "generation", None)
        if not isinstance(generation, str):
            raise ApprovalUnsupported("Approval session has no generation")

        pending_count = sum(
            1
            for entry in _pending_by_approval.values()
            if entry.session_generation == generation
        )
        if pending_count >= MAX_PENDING_APPROVALS_PER_SESSION:
            raise ApprovalLimitReached("Too many pending approvals in this session")

        timeout = self.timeout
        if timeout is None:
            timeout = float(config.project.session_timeout)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        now = datetime.now(timezone.utc)
        requested_at = _iso(now)
        expires_at = _iso(now + timedelta(seconds=timeout))
        expires_at_ts = (now + timedelta(seconds=timeout)).timestamp()

        salt = secrets.token_hex(16)
        record: Dict[str, Any] = {
            "version": 1,
            "revision": 1,
            "id": str(uuid.uuid4()),
            "stepId": self.step.id,
            "threadId": self.step.thread_id,
            "tool": self.tool,
            "title": self.title,
            "status": "pending",
            "requestedBy": session.user.identifier,
            "requestedAt": requested_at,
            "expiresAt": expires_at,
            "editAllowed": False,
            "digestSalt": salt,
            "argumentsDigest": _arguments_digest(self.arguments, salt),
            "argumentsPreview": _arguments_preview(self.arguments),
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

        # Synchronous, before the first await: later Step writes take the lock.
        self.step._approval_managed = True
        emitter = context.emitter
        key = (self.step.thread_id, self.step.id)

        async with step_lock(*key):
            previous_id = _pending_by_step.get(key)
            if previous_id:
                previous = _pending_by_approval.get(previous_id)
                if previous:
                    await _transition_locked(previous, "superseded", reason="replaced")

            # Drain a create/update scheduled before approval management began.
            await self.step.flush_persistence()

            previous_record = self.step.metadata.get(APPROVAL_METADATA_KEY)
            self.step.metadata[APPROVAL_METADATA_KEY] = record
            entry = _Entry(
                approval_id=record["id"],
                thread_id=self.step.thread_id,
                step_id=self.step.id,
                step=self.step,
                future=asyncio.get_running_loop().create_future(),
                emitter=emitter,
                requested_by=session.user.identifier,
                session_generation=generation,
                revision=1,
                expires_at_ts=expires_at_ts,
                arguments=deepcopy(self.arguments),
            )
            if not await _write(entry, record, expected_revision=None):
                self.step.metadata[APPROVAL_METADATA_KEY] = previous_record
                raise ApprovalPersistenceError("Approval persistence failed")

            _pending_by_approval[record["id"]] = entry
            _pending_by_step[key] = record["id"]

        await _emit(entry, record, self.arguments)

        try:
            done, _ = await asyncio.wait([entry.future], timeout=timeout)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(
                    transition(
                        record["id"],
                        "superseded",
                        reason="stopped",
                        retry_on_failure=True,
                    )
                )
            except ApprovalError:
                pass
            raise

        if not done:
            await transition(
                record["id"],
                "expired",
                reason="timeout",
                retry_on_failure=True,
            )

        stored = entry.future.result()
        return _decision_from_record(stored, self.arguments, session.user)
