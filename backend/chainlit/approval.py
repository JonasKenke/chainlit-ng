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
import warnings
import weakref
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Dict,
    Literal,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
    TypeVar,
    Union,
    cast,
)

from pydantic import (
    BaseModel,
    SecretBytes,
    SecretStr,
    TypeAdapter,
    ValidationError,
)

from chainlit.config import config
from chainlit.context import context
from chainlit.data import get_data_layer
from chainlit.logger import logger
from chainlit.session import WebsocketSession
from chainlit.state_store import LockNotAcquired, get_state_store
from chainlit.step import Step
from chainlit.user import PersistedUser, User

if TYPE_CHECKING:
    from chainlit.emitter import BaseChainlitEmitter

APPROVAL_METADATA_KEY = "approval"


def _has_data_layer() -> bool:
    return get_data_layer() is not None


MAX_PENDING_INTERACTIONS_PER_SESSION = 8
# Historical name; approvals and questions share one cap.
MAX_PENDING_APPROVALS_PER_SESSION = MAX_PENDING_INTERACTIONS_PER_SESSION
_TERMINAL_RECORD_LIMIT = 256
# Bidi marks/embeddings/isolates, zero-width and invisible tag characters can
# reorder or hide card text, so typed answers refuse them. Plain text only.
_FORBIDDEN_ANSWER_CHARS = frozenset(
    "\u061c"
    "\u200b\u200c\u200d\u200e\u200f"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069"
    "\ufeff"
)

ApprovalStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "edited",
    "changes_requested",
    "expired",
    "superseded",
    "orphaned",
]
QuestionStatus = Literal["pending", "answered", "expired", "superseded", "orphaned"]
InteractionStatus = Literal[
    "pending",
    "approved",
    "rejected",
    "edited",
    "changes_requested",
    "answered",
    "expired",
    "superseded",
    "orphaned",
]
InteractionKind = Literal["approval", "question"]
Decision = Literal[
    "approve", "reject", "edit", "request_changes", "expired", "superseded", "orphaned"
]

_USER_STATUSES = {"approved", "rejected", "edited", "changes_requested"}
_QUESTION_USER_STATUSES = {"answered"}
_USER_STATUSES_BY_KIND: Dict[str, set[str]] = {
    "approval": _USER_STATUSES,
    "question": _QUESTION_USER_STATUSES,
}
_ALL_USER_STATUSES = _USER_STATUSES | _QUESTION_USER_STATUSES
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
_MAX_CONTENT_CHARS = 16 * 1024
_MAX_COMMENT_CHARS = 500
_MAX_EDIT_FIELDS = 32
_TEXT_FIELD_NAMES = frozenset({"body", "description", "message", "notes", "text"})


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

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        field: Optional[str] = None,
        error_type: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        # Path and type only: the offending value never leaves the server.
        self.field = field
        self.error_type = error_type


class ApprovalEditNotAllowed(ApprovalError):
    code = "ApprovalEditNotAllowed"
    status_code = 422


class ApprovalActionNotAllowed(ApprovalError):
    """The approval's server-enforced policy forbids this decision."""

    code = "ApprovalActionNotAllowed"
    status_code = 400


class ApprovalLimitReached(ApprovalError):
    code = "ApprovalLimitReached"
    status_code = 429


class ApprovalPending(ApprovalError):
    code = "ApprovalPending"
    status_code = 409


class QuestionInvalid(ApprovalError):
    code = "QuestionInvalid"
    status_code = 422


class QuestionAlreadyAsked(ApprovalError):
    code = "QuestionAlreadyAsked"
    status_code = 409


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
    arguments_schema: Optional[type[BaseModel]] = None
    redact: tuple[str, ...] = ()
    decided_arguments: Optional[Dict[str, Any]] = None
    kind: InteractionKind = "approval"
    payload: Dict[str, Any] = field(default_factory=dict)
    remote: bool = False
    shared: bool = False


@dataclass
class _RemoteStep:
    id: str
    thread_id: str
    metadata: Dict[str, Any]


class _NullEmitter:
    async def send_approval(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def send_question(self, *args: Any, **kwargs: Any) -> None:
        pass


# Owner-side registry. Shared pending records and terminal memos live in the
# configured state store; the futures, step objects, and emitters stay here.
_pending_by_approval: Dict[str, _Entry] = {}
_pending_by_step: Dict[tuple[str, str], str] = {}
_step_locks: weakref.WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_terminal_records: Dict[str, tuple[str, InteractionKind, Dict[str, Any]]] = {}

# Reservations let concurrent asks on distinct steps count against the shared
# cap before their first write. Keyed by session generation; asyncio is
# single-threaded, so an unguarded synchronous decrement cannot race.
_reservations: Dict[str, int] = {}
_cap_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)

# Thread ids deleted while an approval step may still write. In-process, like
# the registry: a restart also ends the waiting run. Shortcut: keep the most
# recent tombstones, upgrade if deletes must survive a restart.
_DELETED_THREAD_LIMIT = 4096
_deleted_threads: Dict[str, None] = {}
_managed_thread_users: Dict[str, str] = {}

_PROCESS_DIGEST_KEY = secrets.token_bytes(32)
_JSON_ADAPTER = TypeAdapter(Any)
# The shared memo is an optimization; never let a slow store hold a decision.
_STATE_STORE_TIMEOUT = 1.0
_WORKER_ID = uuid.uuid4().hex
_PENDING_KEY = "approval:pending:"
_STEP_KEY = "approval:step:"
_THREAD_KEY = "approval:thread:"
_SESSION_KEY = "approval:session:"
_OWNER_KEY = "approval:owner:"
_EVENT_KEY = "approval:event:"
_LOCK_KEY = "approval:lock:"
_MANAGED_KEY = "approval:managed:"
_RESERVATION_KEY = "approval:reservation:"
_OWNER_HEARTBEAT_TTL = 6.0
_OWNER_HEARTBEAT_INTERVAL = 2.0
_T = TypeVar("_T")


def _cap_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _cap_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _cap_locks[loop] = lock
    return lock


async def _reserve_capacity(generation: str, approval_id: str, ttl: float) -> None:
    async with _cap_lock():
        pending = sum(
            1
            for entry in _pending_by_approval.values()
            if entry.session_generation == generation
        )
        if pending + _reservations.get(generation, 0) >= (
            MAX_PENDING_INTERACTIONS_PER_SESSION
        ):
            raise ApprovalLimitReached("Too many pending interactions in this session")
        _reservations[generation] = _reservations.get(generation, 0) + 1
    if not getattr(get_state_store(), "shared", False):
        return
    try:
        await _reserve_shared_capacity(generation, approval_id, ttl)
    except ApprovalLimitReached:
        _release_reservation(generation)
        raise
    except Exception:
        # A shared-store outage must not disable the local single-worker path.
        logger.warning("Failed to reserve shared interaction capacity", exc_info=True)


def _release_reservation(generation: str) -> None:
    remaining = _reservations.get(generation, 0) - 1
    if remaining > 0:
        _reservations[generation] = remaining
    else:
        _reservations.pop(generation, None)


def _commit_reservation(generation: str) -> None:
    _release_reservation(generation)


def step_lock(thread_id: str, step_id: str) -> asyncio.Lock:
    """Return the one lock that serializes writes for a step."""
    key = (thread_id, step_id)
    lock = _step_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _step_locks[key] = lock
    return lock


def mark_thread_deleted(thread_id: str) -> None:
    """Tombstone a thread so approval-bearing steps cannot re-create it."""
    _deleted_threads[thread_id] = None
    while len(_deleted_threads) > _DELETED_THREAD_LIMIT:
        _deleted_threads.pop(next(iter(_deleted_threads)))


def clear_thread_deleted(thread_id: str) -> None:
    """Undo a tombstone when the delete itself failed."""
    _deleted_threads.pop(thread_id, None)


def _remember_managed_thread(thread_id: str, identifier: str) -> None:
    _managed_thread_users[thread_id] = identifier
    while len(_managed_thread_users) > _DELETED_THREAD_LIMIT:
        _managed_thread_users.pop(next(iter(_managed_thread_users)))


def is_thread_deleted(thread_id: str) -> bool:
    return thread_id in _deleted_threads


def managed_thread_ids_for(identifier: str) -> set[str]:
    """Threads with an approval step requested by this user."""
    return {
        thread_id
        for thread_id, requested_by in _managed_thread_users.items()
        if requested_by == identifier
    } | {
        entry.thread_id
        for entry in _pending_by_approval.values()
        if entry.requested_by == identifier
    }


async def shared_managed_thread_ids(identifier: str) -> set[str]:
    """Managed thread ids recorded by any worker, when Redis is configured."""
    if not getattr(get_state_store(), "shared", False):
        return set()
    return await _shared_ids(f"{_MANAGED_KEY}{identifier}")


async def _store_managed_thread(thread_id: str, identifier: str) -> None:
    if not getattr(get_state_store(), "shared", False):
        return
    try:
        await _store_call(
            get_state_store().set_add(
                f"{_MANAGED_KEY}{identifier}",
                thread_id,
                ttl=float(config.project.user_session_timeout) + 60.0,
            )
        )
    except Exception:
        logger.warning("Failed to share managed thread state", exc_info=True)


def has_live_interaction(
    interaction_id: str, *, kind: Optional[InteractionKind] = None
) -> bool:
    """True while this process still holds the answerable registry entry."""
    entry = _pending_by_approval.get(interaction_id)
    return entry is not None and (kind is None or entry.kind == kind)


def pending_approvals_for_thread(thread_id: str) -> list[_Entry]:
    return [
        entry for entry in _pending_by_approval.values() if entry.thread_id == thread_id
    ]


async def _shared_live_ids_strict(thread_id: str) -> set[str]:
    """Shared pending set, raising instead of treating an outage as empty."""
    ids = await _store_call(get_state_store().set_members(_thread_key(thread_id)))
    live: set[str] = set()
    for approval_id in ids:
        if await _owner_alive(approval_id) and await _store_call(
            get_state_store().exists(_pending_key(approval_id))
        ):
            live.add(approval_id)
    return live


async def guard_pending_approval(thread_id: Optional[str]) -> None:
    """Refuse a new run while a pending interaction owns the thread.

    An unavailable shared store is unknown state, not "no pending approval":
    fail closed so an approval on another worker cannot be bypassed during an
    outage.
    """
    if not thread_id:
        return
    if pending_approvals_for_thread(thread_id):
        raise ApprovalPending(
            "Resolve the pending approval or question before starting a run"
        )
    try:
        shared = await asyncio.wait_for(
            _shared_live_ids_strict(thread_id),
            timeout=_STATE_STORE_TIMEOUT,
        )
    except Exception as exc:
        logger.warning(
            "Pending-approval state unavailable for thread %s; refusing run",
            thread_id,
            exc_info=True,
        )
        raise ApprovalPending(
            "Cannot verify pending interactions; retry shortly"
        ) from exc
    if shared:
        raise ApprovalPending(
            "Resolve the pending approval or question before starting a run"
        )


async def supersede_thread_approvals(thread_id: str, reason: str = "deleted") -> None:
    """Resolve waiters before a delete; the row is gone, so no write is durable."""
    approval_ids = {
        entry.approval_id for entry in pending_approvals_for_thread(thread_id)
    } | await _shared_ids(_thread_key(thread_id))
    for approval_id in approval_ids:
        try:
            await transition(
                approval_id,
                "superseded",
                reason=reason,
                retry_on_failure=True,
            )
        except ApprovalError:
            continue


async def orphan_session_approvals(generation: str) -> None:
    """Drop waiters that belong to a session that no longer exists."""
    approval_ids = {
        entry.approval_id
        for entry in _pending_by_approval.values()
        if entry.session_generation == generation
    } | await _shared_ids(_session_key(generation))
    for approval_id in approval_ids:
        try:
            await transition(
                approval_id,
                "orphaned",
                reason="session_ended",
                retry_on_failure=True,
            )
        except ApprovalError:
            continue


def _metadata_dict(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, dict) else None


def _set_persisted_approval(
    thread: Dict[str, Any], step_id: str, record: Dict[str, Any]
) -> None:
    for step in thread.get("steps") or []:
        if not isinstance(step, dict) or step.get("id") != step_id:
            continue
        metadata = _metadata_dict(step.get("metadata")) or {}
        step["metadata"] = {**metadata, APPROVAL_METADATA_KEY: deepcopy(record)}
        return


async def orphan_thread_approvals(
    thread: Dict[str, Any], *, keep_generation: Optional[str] = None
) -> None:
    """Mark persisted pending records orphaned, unless this session owns them.

    A page reload or a restarted worker has no live waiter for the old record.
    A same-session reconnect does, and keeps it.
    """
    thread_id = thread.get("id")
    if not isinstance(thread_id, str):
        return

    live = {
        entry.approval_id: entry for entry in pending_approvals_for_thread(thread_id)
    }
    for entry in list(live.values()):
        if keep_generation is not None and entry.session_generation == keep_generation:
            continue
        try:
            record = await transition(
                entry.approval_id,
                "orphaned",
                reason="server_restart",
                retry_on_failure=True,
            )
        except ApprovalError:
            continue
        _set_persisted_approval(thread, entry.step_id, record)

    for approval_id in await _shared_ids(_thread_key(thread_id)):
        if approval_id in live:
            continue
        remote_entry = await _load_pending_entry(approval_id)
        if remote_entry is None:
            continue
        if (
            keep_generation is not None
            and remote_entry.session_generation == keep_generation
        ):
            continue
        try:
            record = await transition(
                approval_id,
                "orphaned",
                reason="server_restart",
                retry_on_failure=True,
            )
        except ApprovalError:
            continue
        _set_persisted_approval(thread, remote_entry.step_id, record)

    data_layer = get_data_layer()
    if data_layer is None:
        return
    for step in thread.get("steps") or []:
        if not isinstance(step, dict):
            continue
        metadata = _metadata_dict(step.get("metadata"))
        persisted_record = metadata.get(APPROVAL_METADATA_KEY) if metadata else None
        if (
            not isinstance(persisted_record, dict)
            or persisted_record.get("status") != "pending"
        ):
            continue
        persisted_id = persisted_record.get("id")
        if not isinstance(persisted_id, str):
            continue
        live_entry = live.get(persisted_id)
        if live_entry and live_entry.session_generation == keep_generation:
            continue
        orphaned = deepcopy(persisted_record)
        orphaned.update(
            {
                "revision": int(persisted_record.get("revision", 1)) + 1,
                "status": "orphaned",
                "decidedBy": None,
                "decidedAt": None,
                "decision": None,
                "reason": None,
                "decisionRequestId": None,
                "terminalReason": "server_restart",
                "terminalAt": _iso(datetime.now(timezone.utc)),
            }
        )
        persisted = await _write_persisted(
            data_layer, thread_id, step, orphaned, persisted_record
        )
        if persisted:
            step["metadata"] = {**(metadata or {}), APPROVAL_METADATA_KEY: orphaned}


async def _write_persisted(
    data_layer: Any,
    thread_id: str,
    step: Dict[str, Any],
    record: Dict[str, Any],
    previous: Dict[str, Any],
) -> bool:
    step_id = step.get("id")
    if not isinstance(step_id, str):
        return False
    try:
        return bool(
            await data_layer.write_approval(
                thread_id=thread_id,
                step_id=step_id,
                record=deepcopy(record),
                expected_revision=previous.get("revision"),
            )
        )
    except Exception:
        logger.exception("Approval orphan persistence failed")
        return False


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


def _redact(value: Any, depth: int = 0, extra_parts: Sequence[str] = ()) -> Any:
    if depth > _MAX_PREVIEW_DEPTH:
        return "[truncated]"
    secret_parts = _SECRET_KEY_PARTS + tuple(
        part.lower().replace("-", "_") for part in extra_parts
    )
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).lower().replace("-", "_")
            if any(part in lower for part in secret_parts):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = _redact(item, depth + 1, extra_parts)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth + 1, extra_parts) for item in value]
    return value


def _arguments_preview(
    arguments: Mapping[str, Any], extra_parts: Sequence[str] = ()
) -> Dict[str, Any]:
    preview = _redact(arguments, extra_parts=extra_parts)
    if len(json.dumps(preview, default=str)) > _MAX_PREVIEW_CHARS:
        return {"truncated": True}
    return preview


def _unwrap_secrets(value: Any) -> Any:
    if isinstance(value, (SecretStr, SecretBytes)):
        return value.get_secret_value()
    if isinstance(value, dict):
        return {key: _unwrap_secrets(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unwrap_secrets(item) for item in value]
    return value


def _validate_edited(arguments: Any, schema: type[BaseModel]) -> Dict[str, Any]:
    """Type/shape validation only; the tool still re-authorizes final args."""
    if not isinstance(arguments, Mapping):
        raise ApprovalInvalidArguments(
            "edited arguments must be an object",
            field="editedArguments",
            error_type="object_type",
        )
    try:
        validated = schema.model_validate(dict(arguments))
    except ValidationError as exc:
        error = exc.errors()[0]
        path = ".".join(str(part) for part in error.get("loc", ()))
        raise ApprovalInvalidArguments(
            "edited arguments are invalid",
            field=f"editedArguments.{path}" if path else "editedArguments",
            # Pydantic's type only; the value and input are never echoed.
            error_type=str(error.get("type", "invalid")),
        ) from None
    try:
        # JSON mode masks SecretStr/SecretBytes; unwrap first so the tool and
        # the digest see the submitted value, not "**********".
        return _JSON_ADAPTER.dump_python(
            _unwrap_secrets(validated.model_dump(mode="python")), mode="json"
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalInvalidArguments(
            "edited arguments must be JSON-compatible",
            field="editedArguments",
            error_type="json_invalid",
        ) from exc


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _is_secret_name(name: str) -> bool:
    lower = name.lower().replace("-", "_")
    return any(part in lower for part in _SECRET_KEY_PARTS)


def _is_secret_field(name: str, prop: Mapping[str, Any]) -> bool:
    """A field whose value is a credential, never sent to the browser."""
    return (
        _is_secret_name(name)
        or prop.get("format") == "password"
        or bool(prop.get("writeOnly"))
    )


def _is_non_editable_field(prop: Mapping[str, Any]) -> bool:
    """Read-only fields keep the original value; the form cannot change them."""
    return prop.get("readOnly") is True


def _unwrap_optional_schema(
    prop: Mapping[str, Any],
) -> tuple[Dict[str, Any], bool]:
    variants = prop.get("anyOf") or prop.get("oneOf")
    if isinstance(variants, list):
        non_null = [
            variant
            for variant in variants
            if isinstance(variant, dict) and variant.get("type") != "null"
        ]
        if len(non_null) == 1 and len(non_null) != len(variants):
            merged = {
                key: value
                for key, value in prop.items()
                if key not in {"anyOf", "oneOf"}
            }
            merged.update(non_null[0])
            return merged, True
    declared = prop.get("type")
    if isinstance(declared, list):
        non_null_types = [value for value in declared if value != "null"]
        if len(non_null_types) == 1:
            merged = {**prop, "type": non_null_types[0]}
            return merged, len(non_null_types) != len(declared)
    return dict(prop), False


def _field_kind(
    name: str, prop: Mapping[str, Any]
) -> Optional[tuple[str, Optional[list[Any]]]]:
    unwrapped, _nullable = _unwrap_optional_schema(prop)
    options = unwrapped.get("enum")
    if isinstance(options, list) and all(
        isinstance(option, (str, int, float, bool)) for option in options
    ):
        return "enum", options
    declared = unwrapped.get("type")
    if declared == "string":
        if (
            unwrapped.get("format") in {"multiline", "textarea"}
            or name in _TEXT_FIELD_NAMES
        ):
            return "text", None
        return "string", None
    if declared == "integer":
        return "integer", None
    if declared == "number":
        return "number", None
    if declared == "boolean":
        return "boolean", None
    if declared == "array":
        items = unwrapped.get("items")
        if isinstance(items, dict):
            item_options = items.get("enum")
            if items.get("type") == "string" and not item_options:
                return "string_list", None
            if isinstance(item_options, list) and all(
                isinstance(option, str) for option in item_options
            ):
                return "string_list", item_options
    return None


def _edit_fields(
    schema: Optional[type[BaseModel]],
) -> Optional[list[Dict[str, Any]]]:
    """Describe schema fields for a form; never include argument values."""
    if schema is None:
        return None
    try:
        json_schema = schema.model_json_schema()
    except Exception:
        return None
    properties = json_schema.get("properties")
    if not isinstance(properties, dict):
        return None
    required = set(json_schema.get("required") or ())
    fields: list[Dict[str, Any]] = []
    for name, raw in properties.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            return None
        kind = _field_kind(name, raw)
        if kind is None:
            return None
        field_kind, options = kind
        _unwrapped, nullable = _unwrap_optional_schema(raw)
        secret = _is_secret_field(name, raw)
        fields.append(
            {
                "name": name,
                "label": str(raw.get("title") or name.replace("_", " ").title())[:80],
                "kind": field_kind,
                "required": name in required,
                "nullable": nullable,
                "secret": secret,
                # Secret values and explicitly read-only fields stay on the
                # server; the form only shows that they are held there.
                "serverHeld": secret or _is_non_editable_field(raw),
                "options": options,
                "description": raw.get("description"),
                "default": raw.get("default"),
            }
        )
        if len(fields) > _MAX_EDIT_FIELDS:
            return None
    if len(json.dumps(fields, default=str)) > _MAX_CONTENT_CHARS:
        return None
    return fields


def _server_held_names(
    schema: Optional[type[BaseModel]],
    fields: Optional[list[Dict[str, Any]]],
    arguments: Mapping[str, Any],
) -> set[str]:
    """Field names whose values never leave the server.

    A schema with editable fields carries the authoritative ``serverHeld``
    flags. A schema that falls back to the raw JSON editor has no field
    descriptors, so top-level secret-looking keys still stay server-held.
    """
    if schema is None:
        return set()
    if fields is not None:
        return {str(field["name"]) for field in fields if field.get("serverHeld")}
    return {str(name) for name in arguments if _is_secret_name(str(name))}


def _client_arguments(
    arguments: Mapping[str, Any], withheld: set[str]
) -> Dict[str, Any]:
    return {
        str(key): value for key, value in arguments.items() if str(key) not in withheld
    }


def _with_server_held_values(
    edited: Any,
    schema: type[BaseModel],
    original: Mapping[str, Any],
) -> Any:
    """Ignore client values for server-held fields and restore originals."""
    if not isinstance(edited, Mapping):
        # Let _validate_edited report object_type without echoing the value.
        return edited
    submitted = dict(edited)
    for name in _server_held_names(schema, _edit_fields(schema), original):
        if name in original:
            submitted[name] = deepcopy(original[name])
        else:
            submitted.pop(name, None)
    return submitted


def _payload(
    record: Mapping[str, Any],
    arguments: Mapping[str, Any],
    edit_fields: Optional[list[Dict[str, Any]]] = None,
    server_held: set[str] | frozenset[str] = frozenset(),
) -> Dict[str, Any]:
    # Records written before the rename only have `title`; it was always
    # secondary text. Emit both keys so old and new clients agree.
    subtitle = record.get("subtitle")
    if subtitle is None:
        subtitle = record.get("title")
    return {
        "id": record["id"],
        "stepId": record["stepId"],
        "threadId": record["threadId"],
        "tool": record["tool"],
        "subtitle": subtitle,
        "title": subtitle,
        "content": record.get("content"),
        "status": record["status"],
        "requestedBy": record.get("requestedBy"),
        "requestedAt": record.get("requestedAt"),
        "expiresAt": record.get("expiresAt"),
        "revision": record.get("revision"),
        "decidedBy": record.get("decidedBy"),
        "decidedAt": record.get("decidedAt"),
        "decision": record.get("decision"),
        "reason": record.get("reason"),
        "terminalReason": record.get("terminalReason"),
        "editAllowed": bool(record.get("editAllowed")),
        "requestChangesAllowed": record.get("requestChangesAllowed") is not False,
        "editFields": edit_fields,
        "argumentsPreview": record.get("argumentsPreview"),
        "editedArgumentsPreview": record.get("editedArgumentsPreview"),
        "arguments": _client_arguments(arguments, set(server_held)),
    }


def _kind(record: Mapping[str, Any]) -> InteractionKind:
    return "question" if record.get("kind") == "question" else "approval"


async def _emit(
    entry: _Entry, record: Mapping[str, Any], arguments: Mapping[str, Any]
) -> None:
    try:
        if entry.kind == "question":
            from chainlit.question import _question_payload

            await entry.emitter.send_question(_question_payload(record))
        else:
            edit_fields = _edit_fields(entry.arguments_schema)
            await entry.emitter.send_approval(
                _payload(
                    record,
                    arguments,
                    edit_fields,
                    _server_held_names(entry.arguments_schema, edit_fields, arguments),
                )
            )
    except Exception:
        logger.exception("Failed to emit %s event", entry.kind)


def _remember_terminal(thread_id: str, record: Dict[str, Any]) -> None:
    _terminal_records[record["id"]] = (
        thread_id,
        _kind(record),
        deepcopy(record),
    )
    while len(_terminal_records) > _TERMINAL_RECORD_LIMIT:
        _terminal_records.pop(next(iter(_terminal_records)))


def _terminal_store_key(approval_id: str) -> str:
    return f"approval:terminal:{approval_id}"


async def _store_terminal(thread_id: str, record: Dict[str, Any]) -> None:
    try:
        await asyncio.wait_for(
            get_state_store().set_json(
                _terminal_store_key(record["id"]),
                {
                    "threadId": thread_id,
                    "kind": _kind(record),
                    "record": record,
                },
                ttl=max(60.0, float(config.project.session_timeout) + 60.0),
            ),
            timeout=_STATE_STORE_TIMEOUT,
        )
    except Exception:
        # The data layer remains authoritative; a shared memo is an optimization.
        logger.warning(
            "Failed to store interaction terminal state",
            exc_info=True,
        )


async def _load_terminal(
    approval_id: str,
    thread_id: Optional[str],
    expected_kind: Optional[InteractionKind],
) -> Optional[tuple[str, InteractionKind, Dict[str, Any]]]:
    try:
        stored = await asyncio.wait_for(
            get_state_store().get_json(_terminal_store_key(approval_id)),
            timeout=_STATE_STORE_TIMEOUT,
        )
    except Exception:
        logger.warning(
            "Failed to read interaction terminal state",
            exc_info=True,
        )
        return None
    if not isinstance(stored, dict):
        return None
    record = stored.get("record")
    stored_thread = stored.get("threadId")
    kind = stored.get("kind")
    if (
        not isinstance(record, dict)
        or not isinstance(stored_thread, str)
        or kind not in ("approval", "question")
    ):
        return None
    if expected_kind is not None and kind != expected_kind:
        return None
    if thread_id is not None and thread_id != stored_thread:
        return None
    return stored_thread, kind, record


async def _raise_missing(
    approval_id: str,
    thread_id: Optional[str],
    expected_kind: Optional[InteractionKind] = None,
) -> NoReturn:
    terminal = _terminal_records.get(approval_id)
    if terminal is None:
        terminal = await _load_terminal(approval_id, thread_id, expected_kind)
    if terminal:
        stored_thread, kind, record = terminal
        if is_thread_deleted(stored_thread):
            # The row is gone; a duplicate submit must not recover the answer.
            raise ApprovalNotFound("Interaction not found")
        if expected_kind is not None and kind != expected_kind:
            raise ApprovalNotFound("Interaction not found")
        if thread_id is not None and thread_id != stored_thread:
            raise ApprovalNotFound("Interaction not found")
        raise ApprovalNotPending("Interaction is no longer pending", record=record)
    raise ApprovalNotFound("Interaction not found")


def _pending_key(approval_id: str) -> str:
    return f"{_PENDING_KEY}{approval_id}"


def _step_key(thread_id: str, step_id: str) -> str:
    return f"{_STEP_KEY}{thread_id}:{step_id}"


def _thread_key(thread_id: str) -> str:
    return f"{_THREAD_KEY}{thread_id}"


def _session_key(generation: str) -> str:
    return f"{_SESSION_KEY}{generation}"


def _owner_key(approval_id: str) -> str:
    return f"{_OWNER_KEY}{approval_id}"


def _event_key(approval_id: str) -> str:
    return f"{_EVENT_KEY}{approval_id}"


def _reservation_key(approval_id: str) -> str:
    return f"{_RESERVATION_KEY}{approval_id}"


def _lock_key(approval_id: str) -> str:
    return f"{_LOCK_KEY}{approval_id}"


async def _store_call(awaitable: Awaitable[_T]) -> _T:
    return await asyncio.wait_for(awaitable, timeout=_STATE_STORE_TIMEOUT)


async def _store_pending(entry: _Entry, record: Dict[str, Any], ttl: float) -> bool:
    """Publish the pending record and its lookup keys for other workers."""
    try:
        store = get_state_store()

        async def write() -> None:
            await store.set_json(
                _pending_key(entry.approval_id),
                {
                    "record": deepcopy(record),
                    "ownerId": _WORKER_ID,
                    "sessionGeneration": entry.session_generation,
                    "expiresAtTs": entry.expires_at_ts,
                    "threadId": entry.thread_id,
                    "stepId": entry.step_id,
                    "kind": entry.kind,
                },
                ttl=ttl,
            )
            await store.set_json(
                _step_key(entry.thread_id, entry.step_id),
                {"id": entry.approval_id},
                ttl=ttl,
            )
            await store.set_add(
                _thread_key(entry.thread_id), entry.approval_id, ttl=ttl
            )
            await store.set(
                _owner_key(entry.approval_id),
                _WORKER_ID.encode(),
                ttl=_OWNER_HEARTBEAT_TTL,
            )

        await asyncio.wait_for(write(), timeout=max(_STATE_STORE_TIMEOUT, 2.0))
        entry.shared = True
        try:
            await _store_call(store.delete(_reservation_key(entry.approval_id)))
        except Exception:
            pass
        return True
    except Exception:
        logger.warning("Failed to share pending interaction state", exc_info=True)
        return False


async def _clear_pending_store(entry: _Entry) -> None:
    if not entry.shared:
        return
    try:
        store = get_state_store()

        async def clear() -> None:
            step_key = _step_key(entry.thread_id, entry.step_id)
            current_step = await store.get_json(step_key)
            if (
                isinstance(current_step, dict)
                and current_step.get("id") == entry.approval_id
            ):
                await store.delete(step_key)
            await store.delete(
                _pending_key(entry.approval_id),
                _owner_key(entry.approval_id),
                _reservation_key(entry.approval_id),
            )
            await store.set_remove(_thread_key(entry.thread_id), entry.approval_id)
            await store.set_remove(
                _session_key(entry.session_generation), entry.approval_id
            )

        await asyncio.wait_for(clear(), timeout=max(_STATE_STORE_TIMEOUT, 2.0))
    except Exception:
        logger.warning(
            "Failed to clear shared pending interaction state", exc_info=True
        )


async def _owner_alive(approval_id: str) -> bool:
    try:
        return await _store_call(get_state_store().exists(_owner_key(approval_id)))
    except Exception as exc:
        raise ApprovalPersistenceError("Interaction registry unavailable") from exc


async def _owner_heartbeat(approval_id: str) -> None:
    warned = False
    while True:
        try:
            await _store_call(
                get_state_store().set(
                    _owner_key(approval_id),
                    _WORKER_ID.encode(),
                    ttl=_OWNER_HEARTBEAT_TTL,
                )
            )
            warned = False
        except asyncio.CancelledError:
            raise
        except Exception:
            if not warned:
                logger.warning("Failed to refresh interaction owner heartbeat")
                warned = True
        await asyncio.sleep(_OWNER_HEARTBEAT_INTERVAL)


def _question_payload(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "prompt": record.get("prompt"),
        "options": record.get("options") or [],
        "allowFreeText": bool(record.get("allowFreeText")),
        "questions": record.get("questions") or [],
        "structured": record.get("structured") is True,
        "multi": record.get("multi") is True,
    }


async def _load_pending_entry(approval_id: str) -> Optional[_Entry]:
    try:
        payload = await _store_call(
            get_state_store().get_json(_pending_key(approval_id))
        )
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    record = payload.get("record")
    thread_id = payload.get("threadId")
    step_id = payload.get("stepId")
    generation = payload.get("sessionGeneration")
    expires_at_ts = payload.get("expiresAtTs")
    if (
        not isinstance(record, dict)
        or record.get("status") != "pending"
        or not isinstance(record.get("id"), str)
        or record["id"] != approval_id
        or not isinstance(thread_id, str)
        or not isinstance(step_id, str)
        or not isinstance(generation, str)
        or not isinstance(expires_at_ts, (int, float))
        or not isinstance(record.get("requestedBy"), str)
    ):
        return None
    kind = _kind(record)
    if kind != payload.get("kind"):
        return None
    remote_step = _RemoteStep(
        id=step_id,
        thread_id=thread_id,
        metadata={APPROVAL_METADATA_KEY: deepcopy(record)},
    )
    entry = _Entry(
        approval_id=approval_id,
        thread_id=thread_id,
        step_id=step_id,
        step=cast(Step, remote_step),
        future=asyncio.get_running_loop().create_future(),
        emitter=cast("BaseChainlitEmitter", _NullEmitter()),
        requested_by=record["requestedBy"],
        session_generation=generation,
        revision=int(record.get("revision", 1)),
        expires_at_ts=float(expires_at_ts),
        kind=kind,
        payload=_question_payload(record) if kind == "question" else {},
        remote=True,
        shared=True,
    )
    return entry


async def _apply_remote_terminal(entry: _Entry, record: Dict[str, Any]) -> None:
    """Apply a record another worker persisted to the local waiter."""
    if entry.future.done():
        return
    entry.step.metadata[APPROVAL_METADATA_KEY] = deepcopy(record)
    entry.revision = int(record.get("revision", entry.revision))
    _pending_by_approval.pop(entry.approval_id, None)
    if _pending_by_step.get((entry.thread_id, entry.step_id)) == entry.approval_id:
        _pending_by_step.pop((entry.thread_id, entry.step_id), None)
    _remember_terminal(entry.thread_id, record)
    await _emit(entry, record, entry.arguments)
    if not entry.future.done():
        entry.future.set_result(record)


async def _watch_interaction(entry: _Entry) -> None:
    """Resolve an owner waiter when another worker commits the decision."""
    try:
        async for raw in get_state_store().subscribe(_event_key(entry.approval_id)):
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            record = event.get("record") if isinstance(event, dict) else None
            if not isinstance(record, dict) or record.get("id") != entry.approval_id:
                continue
            await _apply_remote_terminal(entry, record)
            return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to watch interaction %s", entry.approval_id)


async def _publish_terminal(record: Dict[str, Any]) -> None:
    try:
        await _store_call(
            get_state_store().publish(
                _event_key(record["id"]),
                json.dumps(
                    {"record": record},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode(),
            )
        )
    except Exception:
        logger.warning("Failed to publish interaction terminal state", exc_info=True)


async def _shared_ids(key: str) -> set[str]:
    try:
        return await _store_call(get_state_store().set_members(key))
    except Exception:
        return set()


async def _shared_live_ids_unbounded(thread_id: str) -> set[str]:
    ids = await _shared_ids(_thread_key(thread_id))
    live: set[str] = set()
    for approval_id in ids:
        try:
            if await _owner_alive(approval_id) and await _store_call(
                get_state_store().exists(_pending_key(approval_id))
            ):
                live.add(approval_id)
        except Exception:
            continue
    return live


async def _shared_live_ids(thread_id: str) -> set[str]:
    try:
        return await asyncio.wait_for(
            _shared_live_ids_unbounded(thread_id),
            timeout=_STATE_STORE_TIMEOUT,
        )
    except Exception:
        return set()


async def live_interaction_ids(threads: Sequence[Mapping[str, Any]]) -> set[str]:
    """Ids of pending interactions whose owner worker is still alive."""
    thread_ids = {
        thread_id
        for thread in threads
        if isinstance((thread_id := thread.get("id")), str)
    }
    live = {
        entry.approval_id
        for entry in _pending_by_approval.values()
        if entry.thread_id in thread_ids
    }
    if not getattr(get_state_store(), "shared", False) or not thread_ids:
        return live
    try:
        shared = await asyncio.wait_for(
            asyncio.gather(
                *(_shared_live_ids_unbounded(thread_id) for thread_id in thread_ids)
            ),
            timeout=_STATE_STORE_TIMEOUT * 2,
        )
    except Exception:
        return live
    for ids in shared:
        live.update(ids)
    return live


async def _reserve_shared_capacity(
    generation: str, approval_id: str, ttl: float
) -> None:
    store = get_state_store()
    try:
        async with store.lock(f"{_SESSION_KEY}lock:{generation}", ttl=5, wait=1):
            key = _session_key(generation)
            members = await _store_call(store.set_members(key))
            for member in members:
                pending = await _store_call(store.exists(_pending_key(member)))
                reserved = await _store_call(store.exists(_reservation_key(member)))
                if not pending and not reserved:
                    await _store_call(store.set_remove(key, member))
            members = await _store_call(store.set_members(key))
            if len(members) >= MAX_PENDING_INTERACTIONS_PER_SESSION:
                raise ApprovalLimitReached(
                    "Too many pending interactions in this session"
                )
            await _store_call(store.set_add(key, approval_id, ttl=ttl))
            await _store_call(
                store.set(
                    _reservation_key(approval_id),
                    _WORKER_ID.encode(),
                    ttl=ttl,
                )
            )
    except LockNotAcquired as exc:
        raise ApprovalLimitReached(
            "Too many pending interactions in this session"
        ) from exc


async def _release_shared_capacity(generation: str, approval_id: str) -> None:
    try:
        store = get_state_store()
        await _store_call(store.set_remove(_session_key(generation), approval_id))
        await _store_call(store.delete(_reservation_key(approval_id)))
    except Exception:
        pass


async def _start_shared_pending(
    entry: _Entry, record: Dict[str, Any], timeout: float
) -> tuple[Optional[asyncio.Task], Optional[asyncio.Task]]:
    if not getattr(get_state_store(), "shared", False):
        return None, None
    ttl = max(60.0, timeout + 60.0)
    if not await _store_pending(entry, record, ttl):
        await _release_shared_capacity(entry.session_generation, entry.approval_id)
        return None, None
    heartbeat = asyncio.create_task(_owner_heartbeat(entry.approval_id))
    watcher = asyncio.create_task(_watch_interaction(entry))
    # Let the subscription register before the card becomes answerable.
    await asyncio.sleep(0)
    return heartbeat, watcher


async def _stop_shared_pending(
    tasks: tuple[Optional[asyncio.Task], Optional[asyncio.Task]],
) -> None:
    for task in tasks:
        if task is not None:
            task.cancel()
    await asyncio.gather(
        *(task for task in tasks if task is not None), return_exceptions=True
    )


async def _write(
    entry: _Entry, record: Dict[str, Any], expected_revision: Optional[int]
) -> bool:
    if is_thread_deleted(entry.thread_id):
        return False
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


def _contains_forbidden_text_char(text: str) -> bool:
    """True for control, bidi, zero-width, tag and surrogate characters."""
    for char in text:
        codepoint = ord(char)
        if (
            (codepoint < 32 and char not in "\n\t")
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or 0xE0000 <= codepoint <= 0xE007F
            or char in _FORBIDDEN_ANSWER_CHARS
        ):
            return True
    return False


def _normalize_comment(value: str) -> str:
    """Plain-text decision comment; newlines and tabs are layout, not control."""
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    if len(text) > _MAX_COMMENT_CHARS:
        raise ApprovalInvalidArguments(
            "comment is too long",
            field="reason",
            error_type="too_long",
        )
    if _contains_forbidden_text_char(text):
        raise ApprovalInvalidArguments(
            "comment contains control characters",
            field="reason",
            error_type="control_characters",
        )
    return text


def _validate_question_answer(
    entry: _Entry, answer: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    """Validate against the ask-time snapshot, never mutable metadata."""
    from chainlit.question import (
        _normalize_answers,
        _normalize_field_answer,
        _normalize_question_text,
    )

    if not isinstance(answer, Mapping):
        raise QuestionInvalid("an answer is required")
    questions = entry.payload.get("questions") or []
    if entry.payload.get("structured") is True and questions:
        if "answers" in answer:
            unknown = set(answer) - {"answers"}
            if unknown:
                raise QuestionInvalid("unknown answer field")
            return _normalize_answers(questions, answer.get("answers"))
        if len(questions) > 1:
            raise QuestionInvalid("answers are required")
        return {
            "answers": [
                _normalize_field_answer(
                    questions[0],
                    {
                        "questionId": questions[0]["id"],
                        "optionId": answer.get("optionId"),
                        "text": answer.get("text"),
                    },
                )
            ]
        }

    option_id = answer.get("optionId")
    text = answer.get("text")
    if (option_id is None) == (text is None):
        raise QuestionInvalid("exactly one of optionId or text is required")
    if option_id is not None:
        options = entry.payload.get("options") or []
        match = next(
            (option for option in options if option.get("id") == option_id), None
        )
        if match is None:
            raise QuestionInvalid("unknown option")
        return {"optionId": match["id"], "label": match["label"]}
    if not entry.payload.get("allowFreeText"):
        raise QuestionInvalid("free text is not allowed for this question")
    return {"text": _normalize_question_text(text)}


async def _transition_locked(
    entry: _Entry,
    to_status: InteractionStatus,
    *,
    actor: Optional[User] = None,
    reason: Optional[str] = None,
    decision_request_id: Optional[str] = None,
    retry_on_failure: bool = False,
    edited_arguments: Optional[Mapping[str, Any]] = None,
    answer: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Persist one terminal record for a live pending entry."""
    pending = entry.step.metadata.get(APPROVAL_METADATA_KEY)
    if not isinstance(pending, dict) or pending.get("status") != "pending":
        raise ApprovalNotPending(
            "Interaction is no longer pending",
            record=deepcopy(pending) if isinstance(pending, dict) else None,
        )

    kind = entry.kind
    user_statuses = _USER_STATUSES_BY_KIND[kind]
    if to_status not in user_statuses and to_status not in _SYSTEM_STATUSES:
        raise ValueError(f"Cannot transition a {kind} to {to_status}")

    if to_status in user_statuses and (
        actor is None or actor.identifier != entry.requested_by
    ):
        raise ApprovalNotFound("Interaction not found")

    now = datetime.now(timezone.utc)
    if to_status in user_statuses and now.timestamp() >= entry.expires_at_ts:
        stored = await _transition_locked(
            entry, "expired", reason="timeout", retry_on_failure=retry_on_failure
        )
        raise ApprovalNotPending("Interaction has expired", record=stored)

    if (
        to_status == "changes_requested"
        and pending.get("requestChangesAllowed") is False
    ):
        raise ApprovalActionNotAllowed("This approval does not accept change requests")

    comment: Optional[str] = None
    if to_status in user_statuses:
        comment = _normalize_comment(reason) if isinstance(reason, str) else ""
        comment = comment or None
        if to_status == "changes_requested" and comment is None:
            raise ApprovalInvalidArguments(
                "a comment is required when requesting changes",
                field="reason",
                error_type="missing",
            )

    record = deepcopy(pending)
    final_arguments: Mapping[str, Any] = entry.arguments
    if kind == "question" and to_status == "answered":
        record["answer"] = _validate_question_answer(entry, answer)
    if to_status == "edited":
        if entry.arguments_schema is None:
            raise ApprovalEditNotAllowed("This approval does not accept edits")
        if pending.get("editAllowed") is not True:
            raise ApprovalActionNotAllowed("Editing is disabled for this approval")
        if edited_arguments is None:
            raise ApprovalInvalidArguments(
                "edited arguments are required",
                field="editedArguments",
                error_type="missing",
            )
        validated = _validate_edited(
            _with_server_held_values(
                edited_arguments, entry.arguments_schema, entry.arguments
            ),
            entry.arguments_schema,
        )
        final_arguments = validated
        record.update(
            {
                "editedArgumentsPreview": _arguments_preview(validated, entry.redact),
                "editedArgumentsDigest": _arguments_digest(
                    validated, pending["digestSalt"]
                ),
            }
        )

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
                else "edit"
                if to_status == "edited"
                else "request_changes"
                if to_status == "changes_requested"
                else None
            ),
            "reason": comment if to_status in user_statuses else None,
            "terminalReason": (
                None
                if to_status in user_statuses
                else reason or ("timeout" if to_status == "expired" else None)
            ),
            "terminalAt": _iso(now),
            "decisionRequestId": decision_request_id,
        }
    )

    entry.step.metadata[APPROVAL_METADATA_KEY] = record
    attempts = 3 if retry_on_failure and not is_thread_deleted(entry.thread_id) else 1
    for attempt in range(attempts):
        try:
            written = await _write(
                entry, record, expected_revision=pending.get("revision")
            )
        except asyncio.CancelledError:
            # Cancelled before the write was observed: keep pending. A write
            # that committed anyway stays terminal in storage; the future is
            # never resolved, so nothing executes.
            entry.step.metadata[APPROVAL_METADATA_KEY] = pending
            raise
        if written:
            break
        if attempt + 1 == attempts:
            entry.step.metadata[APPROVAL_METADATA_KEY] = pending
            if retry_on_failure:
                logger.error(
                    "Approval %s terminal state could not be persisted",
                    entry.approval_id,
                )
                break
            if is_thread_deleted(entry.thread_id):
                raise ApprovalNotPending(
                    "Approval was deleted", record=deepcopy(pending)
                )
            raise ApprovalPersistenceError("Approval persistence failed")
        await asyncio.sleep(0.05 * (attempt + 1))

    entry.revision = int(record["revision"])
    entry.decided_arguments = dict(final_arguments) if to_status == "edited" else None
    if to_status == "edited":
        # M11: the trace carries the final, redacted arguments, never raw ones.
        entry.step.input = {
            "approval": {
                "tool": record["tool"],
                "argumentsPreview": record["editedArgumentsPreview"],
                "edited": True,
                "redacted": True,
            }
        }
    _pending_by_approval.pop(entry.approval_id, None)
    if _pending_by_step.get((entry.thread_id, entry.step_id)) == entry.approval_id:
        _pending_by_step.pop((entry.thread_id, entry.step_id), None)
    _remember_terminal(entry.thread_id, record)
    await _store_terminal(entry.thread_id, record)
    await _clear_pending_store(entry)

    if entry.remote:
        await _publish_terminal(record)
        return record

    if not entry.future.done():
        entry.future.set_result(record)
    await _emit(entry, record, final_arguments)
    return record


def _validate_transition_target(
    entry: _Entry,
    *,
    expected_kind: Optional[InteractionKind],
    thread_id: Optional[str],
    session_generation: Optional[str],
) -> None:
    if expected_kind is not None and entry.kind != expected_kind:
        raise ApprovalNotFound("Interaction not found")
    if thread_id is not None and entry.thread_id != thread_id:
        raise ApprovalNotFound("Interaction not found")
    if (
        session_generation is not None
        and entry.session_generation != session_generation
    ):
        raise ApprovalSessionMismatch("Interaction belongs to another session")


async def _transition_serialized(
    approval_id: str,
    to_status: InteractionStatus,
    *,
    actor: Optional[User] = None,
    thread_id: Optional[str] = None,
    session_generation: Optional[str] = None,
    reason: Optional[str] = None,
    decision_request_id: Optional[str] = None,
    retry_on_failure: bool = False,
    edited_arguments: Optional[Mapping[str, Any]] = None,
    expected_kind: Optional[InteractionKind] = None,
    answer: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    entry = _pending_by_approval.get(approval_id)
    if entry is None:
        entry = await _load_pending_entry(approval_id)
        if entry is None:
            await _raise_missing(approval_id, thread_id, expected_kind)
    _validate_transition_target(
        entry,
        expected_kind=expected_kind,
        thread_id=thread_id,
        session_generation=session_generation,
    )
    if entry.remote and to_status in _ALL_USER_STATUSES:
        if not await _owner_alive(approval_id):
            record = await _transition_locked(
                entry,
                "orphaned",
                reason="server_restart",
                retry_on_failure=True,
            )
            raise ApprovalNotPending("Interaction is no longer pending", record=record)
    if entry.remote:
        return await _transition_locked(
            entry,
            to_status,
            actor=actor,
            reason=reason,
            decision_request_id=decision_request_id,
            retry_on_failure=retry_on_failure,
            edited_arguments=edited_arguments,
            answer=answer,
        )

    async with step_lock(entry.thread_id, entry.step_id):
        current = _pending_by_approval.get(approval_id)
        if current is None:
            remote = await _load_pending_entry(approval_id)
            if remote is None:
                await _raise_missing(approval_id, thread_id, expected_kind)
            _validate_transition_target(
                remote,
                expected_kind=expected_kind,
                thread_id=thread_id,
                session_generation=session_generation,
            )
            if to_status in _ALL_USER_STATUSES and not await _owner_alive(approval_id):
                record = await _transition_locked(
                    remote,
                    "orphaned",
                    reason="server_restart",
                    retry_on_failure=True,
                )
                raise ApprovalNotPending(
                    "Interaction is no longer pending", record=record
                )
            return await _transition_locked(
                remote,
                to_status,
                actor=actor,
                reason=reason,
                decision_request_id=decision_request_id,
                retry_on_failure=retry_on_failure,
                edited_arguments=edited_arguments,
                answer=answer,
            )
        _validate_transition_target(
            current,
            expected_kind=expected_kind,
            thread_id=thread_id,
            session_generation=session_generation,
        )
        if current.shared:
            terminal = _terminal_records.get(approval_id)
            if terminal is None:
                terminal = await _load_terminal(approval_id, thread_id, current.kind)
            if terminal is not None:
                # Another worker won while this one waited for the lock.
                await _apply_remote_terminal(current, terminal[2])
                raise ApprovalNotPending(
                    "Interaction is no longer pending", record=terminal[2]
                )
        return await _transition_locked(
            current,
            to_status,
            actor=actor,
            reason=reason,
            decision_request_id=decision_request_id,
            retry_on_failure=retry_on_failure,
            edited_arguments=edited_arguments,
            answer=answer,
        )


async def transition(
    approval_id: str,
    to_status: InteractionStatus,
    *,
    actor: Optional[User] = None,
    thread_id: Optional[str] = None,
    session_generation: Optional[str] = None,
    reason: Optional[str] = None,
    decision_request_id: Optional[str] = None,
    retry_on_failure: bool = False,
    edited_arguments: Optional[Mapping[str, Any]] = None,
    expected_kind: Optional[InteractionKind] = None,
    answer: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The only terminal writer for an interaction.

    A second transition returns the stored record and changes nothing.
    """
    if to_status not in _ALL_USER_STATUSES and to_status not in _SYSTEM_STATUSES:
        raise ValueError(f"Cannot transition an interaction to {to_status}")

    try:
        async with get_state_store().lock(_lock_key(approval_id), ttl=10, wait=2):
            return await _transition_serialized(
                approval_id,
                to_status,
                actor=actor,
                thread_id=thread_id,
                session_generation=session_generation,
                reason=reason,
                decision_request_id=decision_request_id,
                retry_on_failure=retry_on_failure,
                edited_arguments=edited_arguments,
                expected_kind=expected_kind,
                answer=answer,
            )
    except LockNotAcquired as exc:
        raise ApprovalNotPending("Interaction is busy; retry") from exc


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
        else "edit"
        if status == "edited"
        else "request_changes"
        if status == "changes_requested"
        else status
    )
    decided_at = record.get("decidedAt") or record.get("terminalAt") or ""
    return ApprovalDecision(
        approval_id=record["id"],
        thread_id=record["threadId"],
        step_id=record["stepId"],
        decision=decision,
        approved=status in {"approved", "edited"},
        arguments=deepcopy(arguments),
        user=user if status in _USER_STATUSES else None,
        decided_at=decided_at,
        comment=record.get("reason"),
    )


def _interaction_session(
    step: Step,
) -> tuple[Union[User, PersistedUser], str]:
    session = context.session
    if not isinstance(session, WebsocketSession):
        raise ApprovalUnsupported("Structured interactions require a websocket session")
    user = session.user
    if not user or not user.identifier:
        raise ApprovalAuthRequired("An authenticated user is required")
    if step.thread_id != session.thread_id:
        raise ApprovalThreadMismatch("Interaction step belongs to another thread")
    if not session.has_first_interaction:
        raise ApprovalNotReady("Structured interactions require a user message first")
    generation = getattr(session, "generation", None)
    if not isinstance(generation, str):
        raise ApprovalUnsupported("Interaction session has no generation")
    return user, generation


async def _ask(entry: _Entry, timeout: float) -> Dict[str, Any]:
    """Shared wait/timeout/stop path for approvals and questions."""
    try:
        done, _ = await asyncio.wait([entry.future], timeout=timeout)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(
                transition(
                    entry.approval_id,
                    "superseded",
                    reason="stopped",
                    retry_on_failure=True,
                )
            )
        except ApprovalError:
            pass
        raise

    if not done:
        terminal = _terminal_records.get(entry.approval_id)
        if terminal is None:
            terminal = await _load_terminal(
                entry.approval_id, entry.thread_id, entry.kind
            )
        if terminal is not None:
            # Pub/sub is best-effort; the shared terminal record is the fallback.
            await _apply_remote_terminal(entry, terminal[2])
        else:
            try:
                await transition(
                    entry.approval_id,
                    "expired",
                    reason="timeout",
                    retry_on_failure=True,
                )
            except ApprovalNotPending as exc:
                if exc.record is None or exc.record.get("status") == "pending":
                    raise
                await _apply_remote_terminal(entry, exc.record)
    return entry.future.result()


class Approval:
    """A one-shot approval request bound to a Step.

    ``subtitle`` is optional secondary text rendered under the step name, which
    is the card heading; omit it for no subtitle. ``title`` is a deprecated
    alias kept for compatibility.

    The server-held ``arguments`` are the only execution source; a decision
    never carries client-supplied arguments.
    """

    def __init__(
        self,
        *,
        step: Step,
        arguments: Mapping[str, Any],
        tool: Optional[str] = None,
        subtitle: Optional[str] = None,
        title: Optional[str] = None,
        content: Optional[str] = None,
        arguments_schema: Optional[type[BaseModel]] = None,
        redact: Sequence[str] = (),
        timeout: Optional[float] = None,
        allow_edit: Optional[bool] = None,
        allow_request_changes: Optional[bool] = None,
        allow_json_edit: Optional[bool] = None,
    ) -> None:
        if arguments_schema is not None and not (
            isinstance(arguments_schema, type)
            and issubclass(arguments_schema, BaseModel)
        ):
            raise ValueError("arguments_schema must be a pydantic BaseModel")
        if arguments_schema is not None and (
            arguments_schema.model_config.get("extra") != "forbid"
        ):
            raise ValueError(
                "arguments_schema must set model_config = ConfigDict(extra='forbid')"
            )
        for flag_name, flag_value in (
            ("allow_edit", allow_edit),
            ("allow_request_changes", allow_request_changes),
            ("allow_json_edit", allow_json_edit),
        ):
            if flag_value is not None and not isinstance(flag_value, bool):
                raise ValueError(f"{flag_name} must be a bool")
        if title is not None:
            warnings.warn(
                "Approval(title=...) is deprecated; use subtitle=... instead",
                DeprecationWarning,
                stacklevel=2,
            )
            if subtitle is None:
                subtitle = title
            elif subtitle != title:
                raise ValueError(
                    "Approval accepts either subtitle or the deprecated title "
                    "alias, not both"
                )
        self.step = step
        self.arguments = deepcopy(dict(arguments))
        self.tool = tool or step.name
        self.subtitle = subtitle[:200] if subtitle else None
        # Deprecated read alias: the value has always been secondary text.
        self.title = self.subtitle
        if content is not None:
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            content = content.strip()
            if len(content) > _MAX_CONTENT_CHARS:
                raise ValueError(
                    f"content must be at most {_MAX_CONTENT_CHARS} characters"
                )
            content = content or None
        self.content = content
        self.arguments_schema = arguments_schema
        # Editing still requires a schema to validate against; the flag can
        # only remove the action, never add one the server cannot check.
        generated_fields = _edit_fields(arguments_schema)
        self.edit_allowed = (
            arguments_schema is not None
            and allow_edit is not False
            and (generated_fields is not None or allow_json_edit is True)
        )
        self.request_changes_allowed = allow_request_changes is not False
        self.redact = tuple(redact)
        self.timeout = timeout

    async def ask(self) -> ApprovalDecision:
        user, generation = _interaction_session(self.step)

        _canonical_json(self.arguments)

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
            "subtitle": self.subtitle,
            # Deprecated mirror for older frontends that read `title`.
            "title": self.subtitle,
            "content": self.content,
            "status": "pending",
            "requestedBy": user.identifier,
            "requestedAt": requested_at,
            "expiresAt": expires_at,
            "editAllowed": self.edit_allowed,
            "requestChangesAllowed": self.request_changes_allowed,
            "digestSalt": salt,
            "argumentsDigest": _arguments_digest(self.arguments, salt),
            "argumentsPreview": _arguments_preview(self.arguments, self.redact),
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
        _remember_managed_thread(self.step.thread_id, user.identifier)
        await _store_managed_thread(self.step.thread_id, user.identifier)
        emitter = context.emitter
        key = (self.step.thread_id, self.step.id)

        await _reserve_capacity(generation, record["id"], max(60.0, timeout + 60.0))
        reserved = True
        shared_tasks: tuple[Optional[asyncio.Task], Optional[asyncio.Task]] = (
            None,
            None,
        )
        try:
            async with step_lock(*key):
                previous_id = _pending_by_step.get(key)
                if previous_id:
                    previous = _pending_by_approval.get(previous_id)
                    if previous:
                        await _transition_locked(
                            previous, "superseded", reason="replaced"
                        )

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
                    requested_by=user.identifier,
                    session_generation=generation,
                    revision=1,
                    expires_at_ts=expires_at_ts,
                    arguments=deepcopy(self.arguments),
                    arguments_schema=self.arguments_schema,
                    redact=self.redact,
                )
                if not await _write(entry, record, expected_revision=None):
                    if previous_record is None:
                        self.step.metadata.pop(APPROVAL_METADATA_KEY, None)
                    else:
                        self.step.metadata[APPROVAL_METADATA_KEY] = previous_record
                    raise ApprovalPersistenceError("Approval persistence failed")

                _pending_by_approval[record["id"]] = entry
                _pending_by_step[key] = record["id"]
                _commit_reservation(generation)
                reserved = False

            shared_tasks = await _start_shared_pending(entry, record, timeout)
            await _emit(entry, record, self.arguments)
            stored = await _ask(entry, timeout)
        finally:
            if reserved:
                _release_reservation(generation)
                await _release_shared_capacity(generation, record["id"])
            await _stop_shared_pending(shared_tasks)

        if stored["status"] == "edited":
            # Persist the final-argument trace; the record write already landed.
            await self.step.update()
        return _decision_from_record(
            stored,
            self.arguments
            if entry.decided_arguments is None
            else entry.decided_arguments,
            user,
        )


# Shared interaction family. An approval and a question are different public
# contracts over one state machine; apps can catch the readable names.
QuestionError = ApprovalError
QuestionAuthRequired = ApprovalAuthRequired
QuestionUnsupported = ApprovalUnsupported
QuestionThreadMismatch = ApprovalThreadMismatch
QuestionNotReady = ApprovalNotReady
QuestionLimitReached = ApprovalLimitReached
QuestionPersistenceError = ApprovalPersistenceError
QuestionNotPending = ApprovalNotPending
