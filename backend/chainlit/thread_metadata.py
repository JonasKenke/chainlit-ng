"""Filter thread metadata at browser response boundaries."""

import json
from typing import Any, Dict, Mapping, Optional, Set, cast

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
    "questions",
    "structured",
    "multi",
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


def _public_question_answer(answer: Any) -> Any:
    """Redact typed answers; keep server option ids and labels."""
    if answer is None:
        return None
    if not isinstance(answer, Mapping):
        return {"text": None, "redacted": True}
    raw_answers = answer.get("answers")
    if isinstance(raw_answers, list):
        public_answers = []
        for item in raw_answers:
            if not isinstance(item, Mapping) or not isinstance(
                item.get("questionId"), str
            ):
                continue
            question_id = item["questionId"]
            option_ids = item.get("optionIds")
            if isinstance(option_ids, list) and all(
                isinstance(option_id, str) for option_id in option_ids
            ):
                labels = item.get("labels")
                public_item: Dict[str, Any] = {
                    "questionId": question_id,
                    "optionIds": option_ids,
                    "labels": labels if isinstance(labels, list) else [],
                }
                if item.get("text") is not None:
                    public_item["text"] = None
                    public_item["redacted"] = True
                public_answers.append(public_item)
            elif isinstance(item.get("optionId"), str) and item.get("text") is None:
                public_answers.append(
                    {
                        "questionId": question_id,
                        "optionId": item["optionId"],
                        "label": item.get("label"),
                    }
                )
            else:
                public_answers.append(
                    {"questionId": question_id, "text": None, "redacted": True}
                )
        return {"answers": public_answers}
    if isinstance(answer.get("optionId"), str) and answer.get("text") is None:
        return {
            "optionId": answer["optionId"],
            "label": answer.get("label"),
        }
    return {"text": None, "redacted": True}


def _public_question(
    record: Mapping[str, Any], live_ids: Optional[Set[str]] = None
) -> Optional[dict]:
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

    # A default is app configuration that may carry account data; the share
    # view is read-only and never needs it.
    if isinstance(public.get("questions"), list):
        public["questions"] = [
            {key: value for key, value in spec.items() if key != "default"}
            for spec in public["questions"]
            if isinstance(spec, Mapping)
        ]

    # M1: a typed answer never crosses the share boundary. An option answer
    # keeps only the server id and label; any other shape is redacted.
    public["answer"] = _public_question_answer(record.get("answer"))

    # A pending record is only answerable while this process holds its live
    # registry entry. Projection only: the stored pending row is not changed.
    live = (
        cast(str, record["id"]) in live_ids
        if live_ids is not None
        else has_live_interaction(cast(str, record["id"]), kind="question")
    )
    if public.get("status") == "pending" and not live:
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


def _input_has_interaction(step: Mapping[str, Any]) -> bool:
    """True when the redacted interaction preview marks this step.

    The input trace survives a failed metadata write, so it is an independent
    marker that the step is an approval or question interaction.
    """
    raw = step.get("input")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return False
    return isinstance(raw, Mapping) and "approval" in raw


def _drop_interaction_trace(step: Mapping[str, Any]) -> dict:
    """Drop the input and output of an interaction from a public projection."""
    return {key: value for key, value in step.items() if key not in ("input", "output")}


def _public_steps(steps: Any, live_ids: Optional[Set[str]] = None) -> Any:
    if not isinstance(steps, list):
        return []
    result = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        interaction_input = _input_has_interaction(step)
        raw_metadata = step.get("metadata")
        if isinstance(raw_metadata, str):
            try:
                raw_metadata = json.loads(raw_metadata)
            except (TypeError, ValueError):
                raw_metadata = None
        if not isinstance(raw_metadata, dict):
            # Without parseable metadata we cannot tell whether the step is an
            # interaction; drop the input trace as well as the metadata.
            public_step = {
                key: value
                for key, value in step.items()
                if key not in ("input", "metadata")
            }
            if interaction_input:
                public_step.pop("output", None)
            result.append({**public_step, "metadata": {}})
            continue
        if "approval" not in raw_metadata:
            public_step = _drop_interaction_trace(step) if interaction_input else step
            result.append({**public_step, "metadata": raw_metadata})
            continue
        approval = raw_metadata["approval"]
        # The framework writes the edited preview into the step input trace
        # (M11); the public view must not carry it past the allowlist. The
        # step output may repeat the decision and comment.
        public_step = _drop_interaction_trace(step)
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
            public_record = _public_question(approval, live_ids)
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


def _project_owner_steps(steps: Any, live_ids: Optional[Set[str]] = None) -> Any:
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
        if not isinstance(record, dict):
            result.append(step)
            continue
        live = (
            cast(str, record.get("id")) in live_ids
            if live_ids is not None
            else has_live_interaction(cast(str, record.get("id")), kind="question")
        )
        if (
            record.get("kind") == "question"
            and record.get("status") == "pending"
            and not live
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
    live_ids: Optional[Set[str]] = None,
    strip_storage_keys: bool = False,
) -> Optional[ThreadDict]:
    """Copy a thread with only browser-visible metadata, preserving stored state.

    Args:
        thread: Internal thread returned by the data layer, or None.
        include_settings: Include profile and settings for an owner's chat view.
        include_owner: Keep the owner identity. False also drops the DynamoDB
            internal keys that carry the owner's identifier.
        live_ids: Pending interaction ids with a live owner. When omitted,
            liveness falls back to this process's local registry.
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
            _project_owner_steps(_normalize_steps(steps), live_ids)
            if include_settings
            else _public_steps(steps, live_ids)
        )
    if not include_owner:
        # A Dynamo thread carries its item keys inside each nested row; the
        # share payload must not expose the storage shape (the top-level keys
        # are removed above).
        public_steps = result.get("steps")
        if isinstance(public_steps, list):
            result["steps"] = [
                {key: value for key, value in step.items() if key not in ("PK", "SK")}
                if isinstance(step, dict)
                else step
                for step in public_steps
            ]
    if (not include_owner or strip_storage_keys) and isinstance(
        result.get("elements"), list
    ):
        blocked = {"PK", "SK", "objectKey"}
        result["elements"] = [
            {key: value for key, value in element.items() if key not in blocked}
            if isinstance(element, dict)
            else element
            for element in cast(list, result["elements"])
        ]
    return cast(ThreadDict, result)
