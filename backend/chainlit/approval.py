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
import re
import secrets
import uuid
import weakref
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Literal,
    Mapping,
    NoReturn,
    Optional,
    Sequence,
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
from chainlit.step import Step
from chainlit.user import PersistedUser, User

if TYPE_CHECKING:
    from chainlit.emitter import BaseChainlitEmitter

APPROVAL_METADATA_KEY = "approval"
MAX_PENDING_INTERACTIONS_PER_SESSION = 8
# Historical name; approvals and questions share one cap.
MAX_PENDING_APPROVALS_PER_SESSION = MAX_PENDING_INTERACTIONS_PER_SESSION
_TERMINAL_RECORD_LIMIT = 256
_MAX_QUESTION_PROMPT = 2000
_MAX_QUESTION_ANSWER = 2000
_MAX_QUESTION_OPTIONS = 8
_MAX_OPTION_LABEL = 200
_MAX_OPTION_DESCRIPTION = 500
_OPTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
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
class QuestionOption:
    id: str
    label: str
    description: Optional[str] = None


@dataclass(frozen=True)
class QuestionAnswer:
    question_id: str
    thread_id: str
    step_id: str
    status: QuestionStatus
    option_id: Optional[str]
    label: Optional[str]
    text: Optional[str]
    user: Optional[User]
    answered_at: str
    durable: bool

    @property
    def answered(self) -> bool:
        return self.status == "answered"

    def as_tool_result(self) -> Dict[str, Any]:
        """Provenance-marked payload; the answer is untrusted user input."""
        return {
            "provenance": "chainlit.question_answer",
            "trust": "untrusted_user_input",
            "questionId": self.question_id,
            "status": self.status,
            "optionId": self.option_id,
            "text": self.text,
            "durable": self.durable,
        }


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


# Module-level, in-process registry. S1 runs in one worker with sticky
# sessions (KRE-188 tracks multi-worker).
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


def _cap_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _cap_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _cap_locks[loop] = lock
    return lock


async def _reserve_capacity(generation: str) -> None:
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


def guard_pending_approval(thread_id: Optional[str]) -> None:
    """Refuse a new run while a pending interaction owns the thread."""
    if thread_id and pending_approvals_for_thread(thread_id):
        raise ApprovalPending(
            "Resolve the pending approval or question before starting a run"
        )


async def supersede_thread_approvals(thread_id: str, reason: str = "deleted") -> None:
    """Resolve waiters before a delete; the row is gone, so no write is durable."""
    for entry in pending_approvals_for_thread(thread_id):
        try:
            await transition(
                entry.approval_id,
                "superseded",
                reason=reason,
                retry_on_failure=True,
            )
        except ApprovalError:
            continue


async def orphan_session_approvals(generation: str) -> None:
    """Drop waiters that belong to a session that no longer exists."""
    entries = [
        entry
        for entry in _pending_by_approval.values()
        if entry.session_generation == generation
    ]
    for entry in entries:
        try:
            await transition(
                entry.approval_id,
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
        approval_id = persisted_record.get("id")
        if not isinstance(approval_id, str):
            continue
        live_entry = live.get(approval_id)
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
    return {
        "id": record["id"],
        "stepId": record["stepId"],
        "threadId": record["threadId"],
        "tool": record["tool"],
        "title": record.get("title"),
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


def _question_payload(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "question",
        "id": record["id"],
        "stepId": record["stepId"],
        "threadId": record["threadId"],
        "status": record["status"],
        "prompt": record.get("prompt"),
        "options": deepcopy(record.get("options") or []),
        "allowFreeText": bool(record.get("allowFreeText")),
        "requestedAt": record.get("requestedAt"),
        "expiresAt": record.get("expiresAt"),
        "revision": record.get("revision"),
        "decidedBy": record.get("decidedBy"),
        "decidedAt": record.get("decidedAt"),
        "answer": deepcopy(record.get("answer")),
        "terminalReason": record.get("terminalReason"),
        "terminalAt": record.get("terminalAt"),
    }


async def _emit(
    entry: _Entry, record: Mapping[str, Any], arguments: Mapping[str, Any]
) -> None:
    try:
        if entry.kind == "question":
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


def _raise_missing(
    approval_id: str,
    thread_id: Optional[str],
    expected_kind: Optional[InteractionKind] = None,
) -> NoReturn:
    terminal = _terminal_records.get(approval_id)
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


def _normalize_question_text(value: Any) -> str:
    if not isinstance(value, str):
        raise QuestionInvalid("answer text must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text) > _MAX_QUESTION_ANSWER:
        raise QuestionInvalid("answer text must be 1..2000 characters")
    if _contains_forbidden_text_char(text):
        raise QuestionInvalid("answer text contains control characters")
    return text


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
    if not isinstance(answer, Mapping):
        raise QuestionInvalid("an answer is required")
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

    if not entry.future.done():
        entry.future.set_result(record)
    await _emit(entry, record, final_arguments)
    return record


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

    entry = _pending_by_approval.get(approval_id)
    if entry is None:
        _raise_missing(approval_id, thread_id, expected_kind)
    if expected_kind is not None and entry.kind != expected_kind:
        raise ApprovalNotFound("Interaction not found")

    if thread_id is not None and entry.thread_id != thread_id:
        raise ApprovalNotFound("Interaction not found")
    if (
        session_generation is not None
        and entry.session_generation != session_generation
    ):
        raise ApprovalSessionMismatch("Interaction belongs to another session")

    async with step_lock(entry.thread_id, entry.step_id):
        current = _pending_by_approval.get(approval_id)
        if current is None:
            _raise_missing(approval_id, thread_id, expected_kind)
        if expected_kind is not None and current.kind != expected_kind:
            raise ApprovalNotFound("Interaction not found")
        if thread_id is not None and current.thread_id != thread_id:
            raise ApprovalNotFound("Interaction not found")
        if (
            session_generation is not None
            and current.session_generation != session_generation
        ):
            raise ApprovalSessionMismatch("Interaction belongs to another session")
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
        await transition(
            entry.approval_id,
            "expired",
            reason="timeout",
            retry_on_failure=True,
        )
    return entry.future.result()


def _validated_question_payload(
    question: str,
    options: Sequence[QuestionOption],
    allow_free_text: bool,
) -> Dict[str, Any]:
    if not isinstance(question, str):
        raise QuestionInvalid("question must be a string")
    prompt = question.strip()
    if not prompt or len(prompt) > _MAX_QUESTION_PROMPT:
        raise QuestionInvalid("question must be 1..2000 characters")
    if not isinstance(allow_free_text, bool):
        raise QuestionInvalid("allow_free_text must be a boolean")
    if options is None:
        options = ()
    if isinstance(options, (str, bytes)) or not isinstance(options, Sequence):
        raise QuestionInvalid("options must be a sequence")
    if len(options) > _MAX_QUESTION_OPTIONS:
        raise QuestionInvalid("at most 8 options are allowed")

    normalized: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for option in options:
        if isinstance(option, QuestionOption):
            option_id, label, description = (
                option.id,
                option.label,
                option.description,
            )
        elif isinstance(option, Mapping):
            option_id = option.get("id")
            label = option.get("label")
            description = option.get("description")
        else:
            raise QuestionInvalid("options must be QuestionOption values")
        if not isinstance(option_id, str) or not _OPTION_ID_PATTERN.match(option_id):
            raise QuestionInvalid("invalid option id")
        if option_id in seen:
            raise QuestionInvalid("duplicate option id")
        seen.add(option_id)
        if not isinstance(label, str) or not label.strip():
            raise QuestionInvalid("option label is required")
        label = label.strip()
        if len(label) > _MAX_OPTION_LABEL:
            raise QuestionInvalid("option label is too long")
        if description is not None:
            if not isinstance(description, str):
                raise QuestionInvalid("option description must be a string")
            description = description.strip()
            if len(description) > _MAX_OPTION_DESCRIPTION:
                raise QuestionInvalid("option description is too long")
        normalized.append({"id": option_id, "label": label, "description": description})

    if not allow_free_text and not normalized:
        raise QuestionInvalid("a closed question needs at least one option")
    return {
        "prompt": prompt,
        "options": normalized,
        "allowFreeText": allow_free_text,
    }


def _question_answer_from_record(
    record: Mapping[str, Any], user: Optional[User]
) -> QuestionAnswer:
    answer = record.get("answer") or {}
    status = cast(QuestionStatus, record["status"])
    return QuestionAnswer(
        question_id=record["id"],
        thread_id=record["threadId"],
        step_id=record["stepId"],
        status=status,
        option_id=answer.get("optionId"),
        label=answer.get("label"),
        text=answer.get("text"),
        user=user if status == "answered" else None,
        answered_at=record.get("decidedAt") or record.get("terminalAt") or "",
        durable=get_data_layer() is not None and status == "answered",
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
        content: Optional[str] = None,
        arguments_schema: Optional[type[BaseModel]] = None,
        redact: Sequence[str] = (),
        timeout: Optional[float] = None,
        allow_edit: Optional[bool] = None,
        allow_request_changes: Optional[bool] = None,
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
        ):
            if flag_value is not None and not isinstance(flag_value, bool):
                raise ValueError(f"{flag_name} must be a bool")
        self.step = step
        self.arguments = deepcopy(dict(arguments))
        self.tool = tool or step.name
        self.title = title[:200] if title else None
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
        self.edit_allowed = arguments_schema is not None and allow_edit is not False
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
            "title": self.title,
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
        emitter = context.emitter
        key = (self.step.thread_id, self.step.id)

        await _reserve_capacity(generation)
        reserved = True
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

            await _emit(entry, record, self.arguments)
            stored = await _ask(entry, timeout)
        finally:
            if reserved:
                _release_reservation(generation)

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


async def ask(
    question: str,
    *,
    options: Sequence[QuestionOption] = (),
    allow_free_text: bool = True,
    timeout: Optional[float] = None,
    step: Optional[Step] = None,
) -> QuestionAnswer:
    """Ask the user a structured question and wait for one answer."""
    payload = _validated_question_payload(question, options, allow_free_text)
    target = step or context.current_step
    if target is None:
        raise ApprovalUnsupported("No current step to bind the question to")

    user, generation = _interaction_session(target)
    if timeout is None:
        timeout = float(config.project.session_timeout)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")

    now = datetime.now(timezone.utc)
    record: Dict[str, Any] = {
        "version": 1,
        "revision": 1,
        "kind": "question",
        "id": str(uuid.uuid4()),
        "stepId": target.id,
        "threadId": target.thread_id,
        "status": "pending",
        "prompt": payload["prompt"],
        # Separate copy: step.metadata must not alias the ask-time snapshot.
        "options": deepcopy(payload["options"]),
        "allowFreeText": payload["allowFreeText"],
        "requestedBy": user.identifier,
        "requestedAt": _iso(now),
        "expiresAt": _iso(now + timedelta(seconds=timeout)),
        "answer": None,
        "decidedBy": None,
        "decidedAt": None,
        "decision": None,
        "decisionRequestId": None,
        "terminalReason": None,
        "terminalAt": None,
    }
    expires_at_ts = (now + timedelta(seconds=timeout)).timestamp()

    # Synchronous, before the first await: later Step writes take the lock.
    target._approval_managed = True
    _remember_managed_thread(target.thread_id, user.identifier)
    emitter = context.emitter
    key = (target.thread_id, target.id)
    entry = _Entry(
        approval_id=record["id"],
        thread_id=target.thread_id,
        step_id=target.id,
        step=target,
        future=asyncio.get_running_loop().create_future(),
        emitter=emitter,
        requested_by=user.identifier,
        session_generation=generation,
        revision=1,
        expires_at_ts=expires_at_ts,
        kind="question",
        payload=deepcopy(payload),
    )

    await _reserve_capacity(generation)
    reserved = True
    try:
        async with step_lock(*key):
            # L4: a terminal record in the one slot must not be overwritten.
            # A pending record is superseded below.
            previous_record = target.metadata.get(APPROVAL_METADATA_KEY)
            if isinstance(previous_record, dict) and (
                previous_record.get("status") != "pending"
            ):
                raise QuestionAlreadyAsked(
                    "This step already asked a terminal question"
                )

            previous_id = _pending_by_step.get(key)
            if previous_id:
                previous = _pending_by_approval.get(previous_id)
                if previous:
                    await _transition_locked(previous, "superseded", reason="replaced")

            # Drain a create/update scheduled before interaction management began.
            await target.flush_persistence()

            target.metadata[APPROVAL_METADATA_KEY] = record
            if not await _write(entry, record, expected_revision=None):
                if previous_record is None:
                    target.metadata.pop(APPROVAL_METADATA_KEY, None)
                else:
                    target.metadata[APPROVAL_METADATA_KEY] = previous_record
                raise ApprovalPersistenceError("Question persistence failed")

            _pending_by_approval[record["id"]] = entry
            _pending_by_step[key] = record["id"]
            _commit_reservation(generation)
            reserved = False

        await _emit(entry, record, {})
        stored = await _ask(entry, timeout)
        return _question_answer_from_record(stored, user)
    finally:
        if reserved:
            _release_reservation(generation)


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
