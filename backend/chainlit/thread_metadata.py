"""Filter thread metadata at browser response boundaries."""

import json
from typing import Any, Mapping, Optional, cast

from chainlit.approval import has_live_interaction
from chainlit.types import ThreadDict

# Approval detail that may cross the public share/list boundary. Arguments,
# digests, salts, request ids and user identifiers stay on the owner view.
_PUBLIC_APPROVAL_FIELDS = (
    "status",
    "tool",
    "decision",
    "decidedAt",
    "terminalReason",
)

# Question detail a read-only card needs. `answer.text` is redacted below;
# identities, expiry, CAS revision and request ids stay on the owner view.
_PUBLIC_QUESTION_FIELDS = (
    "kind",
    "id",
    "stepId",
    "threadId",
    "version",
    "status",
    "prompt",
    "options",
    "allowFreeText",
    "answer",
    "decidedAt",
    "terminalReason",
    "terminalAt",
)


def _record_kind(record: Mapping[str, Any]) -> Optional[str]:
    kind = record.get("kind", "approval")
    if kind not in ("approval", "question"):
        return None
    return cast(str, kind)


def _public_question(record: Mapping[str, Any]) -> Optional[dict]:
    """Return the share-safe IQuestion, or None when the shape is not valid."""
    valid = (
        isinstance(record.get("id"), str)
        and isinstance(record.get("stepId"), str)
        and isinstance(record.get("threadId"), str)
        and isinstance(record.get("version"), int)
        and isinstance(record.get("status"), str)
        and isinstance(record.get("prompt"), str)
        and isinstance(record.get("options"), list)
        and isinstance(record.get("allowFreeText"), bool)
        and "terminalAt" in record
    )
    if not valid:
        return None

    public = {key: record[key] for key in _PUBLIC_QUESTION_FIELDS if key in record}
    public["kind"] = "question"

    # M1: a typed answer never crosses the share boundary. An option answer
    # keeps only the server id and label; any other shape is redacted.
    answer = record.get("answer")
    if answer is None:
        public["answer"] = None
    elif (
        isinstance(answer, Mapping)
        and isinstance(answer.get("optionId"), str)
        and answer.get("text") is None
    ):
        public["answer"] = {
            "optionId": answer["optionId"],
            "label": answer.get("label"),
        }
    else:
        public["answer"] = {"text": None, "redacted": True}

    # A pending record is only answerable while this process holds its live
    # registry entry. Projection only: the stored pending row is not changed.
    if public.get("status") == "pending" and not has_live_interaction(
        cast(str, record["id"]), kind="question"
    ):
        public["status"] = "orphaned"
    return public


def _metadata_dict(raw: Any) -> dict:
    """Parse JSON columns that SQLite hands back as strings."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _normalize_steps(steps: Any) -> Any:
    """Ensure reloaded steps carry metadata as an object, not a JSON string."""
    if not isinstance(steps, list):
        return steps
    return [
        {**step, "metadata": _metadata_dict(step.get("metadata"))}
        if isinstance(step, dict)
        else step
        for step in steps
    ]


def _public_steps(steps: Any) -> Any:
    if not isinstance(steps, list):
        return []
    result = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw_metadata = step.get("metadata")
        if isinstance(raw_metadata, str):
            try:
                raw_metadata = json.loads(raw_metadata)
            except (TypeError, ValueError):
                raw_metadata = None
        if not isinstance(raw_metadata, dict):
            # Without parseable metadata we cannot tell whether the step is an
            # interaction; drop the input trace as well as the metadata.
            result.append(
                {
                    key: value
                    for key, value in step.items()
                    if key not in ("input", "metadata")
                }
                | {"metadata": {}}
            )
            continue
        if "approval" not in raw_metadata:
            result.append({**step, "metadata": raw_metadata})
            continue
        approval = raw_metadata["approval"]
        # The framework writes the edited preview into the step input trace
        # (M11); the public view must not carry it past the allowlist.
        public_step = {key: value for key, value in step.items() if key != "input"}
        if not isinstance(approval, Mapping):
            # An unexpected shape may carry detail outside the nested record;
            # drop the whole step metadata rather than guessing.
            result.append({**public_step, "metadata": {}})
            continue
        kind = _record_kind(approval)
        if kind is None:
            # Unknown kind: drop the interaction record, keep other metadata.
            result.append(
                {
                    **public_step,
                    "metadata": {
                        key: value
                        for key, value in raw_metadata.items()
                        if key != "approval"
                    },
                }
            )
            continue
        if kind == "question":
            public_record = _public_question(approval)
        else:
            public_record = {
                key: approval[key] for key in _PUBLIC_APPROVAL_FIELDS if key in approval
            }
        if public_record is None:
            result.append(
                {
                    **public_step,
                    "metadata": {
                        key: value
                        for key, value in raw_metadata.items()
                        if key != "approval"
                    },
                }
            )
            continue
        result.append(
            {
                **public_step,
                "metadata": {**raw_metadata, "approval": public_record},
            }
        )
    return result


def _project_owner_steps(steps: Any) -> Any:
    """Project orphaned questions for the owner without filtering the record."""
    if not isinstance(steps, list):
        return steps
    result = []
    for step in steps:
        if not isinstance(step, dict):
            result.append(step)
            continue
        metadata = step.get("metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (TypeError, ValueError):
                result.append(step)
                continue
        if not isinstance(metadata, dict):
            result.append(step)
            continue
        record = metadata.get("approval")
        if (
            isinstance(record, dict)
            and record.get("kind") == "question"
            and record.get("status") == "pending"
            and not has_live_interaction(cast(str, record.get("id")), kind="question")
        ):
            result.append(
                {
                    **step,
                    "metadata": {
                        **metadata,
                        "approval": {**record, "status": "orphaned"},
                    },
                }
            )
            continue
        result.append(step)
    return result


def public_thread(
    thread: Optional[ThreadDict],
    *,
    include_settings: bool = False,
    include_owner: bool = True,
) -> Optional[ThreadDict]:
    """Copy a thread with only browser-visible metadata, preserving stored state.

    Args:
        thread: Internal thread returned by the data layer, or None.
        include_settings: Include profile and settings for an owner's chat view.
        include_owner: Keep the owner identity. False also drops the DynamoDB
            internal keys that carry the owner's identifier.
    """
    if thread is None:
        return None

    metadata = _metadata_dict(thread.get("metadata"))

    allowed = {"is_shared", "viewer_read_only"}
    if include_settings:
        allowed.update({"chat_profile", "chat_settings"})

    result = {
        **thread,
        "metadata": {key: value for key, value in metadata.items() if key in allowed},
    }
    if not include_owner:
        result["userId"] = None
        result["userIdentifier"] = None
        for key in ("PK", "SK", "UserThreadPK", "UserThreadSK"):
            result.pop(key, None)
    if "steps" in thread:
        steps = thread.get("steps")
        result["steps"] = (
            _project_owner_steps(_normalize_steps(steps))
            if include_settings
            else _public_steps(steps)
        )
    return cast(ThreadDict, result)
