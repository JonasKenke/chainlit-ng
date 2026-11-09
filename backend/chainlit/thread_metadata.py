"""Filter thread metadata at browser response boundaries."""

import json
from typing import Any, Optional, cast

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
    steps = _normalize_steps(steps)
    if not isinstance(steps, list):
        return steps
    result = []
    for step in steps:
        if not isinstance(step, dict):
            result.append(step)
            continue
        raw_metadata: dict = step["metadata"]
        if "approval" not in raw_metadata:
            result.append(step)
            continue
        approval = raw_metadata["approval"]
        # The framework writes the edited preview into the step input trace
        # (M11); the public view must not carry it past the whitelist.
        public_step: dict = {
            key: value for key, value in step.items() if key != "input"
        }
        if not isinstance(approval, dict):
            # An unexpected shape may carry detail outside the nested record;
            # drop the whole step metadata rather than guessing.
            result.append({**public_step, "metadata": {}})
            continue
        result.append(
            {
                **public_step,
                "metadata": {
                    **raw_metadata,
                    "approval": {
                        key: approval[key]
                        for key in _PUBLIC_APPROVAL_FIELDS
                        if key in approval
                    },
                },
            }
        )
    return result


def public_thread(
    thread: Optional[ThreadDict], *, include_settings: bool = False
) -> Optional[ThreadDict]:
    """Copy a thread with only browser-visible metadata, preserving stored state.

    Args:
        thread: Internal thread returned by the data layer, or None.
        include_settings: Include profile and settings for an owner's chat view.
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
    if "steps" in thread:
        steps = thread.get("steps")
        result["steps"] = (
            _normalize_steps(steps) if include_settings else _public_steps(steps)
        )
    return cast(ThreadDict, result)
