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


def _public_steps(steps: Any) -> Any:
    if not isinstance(steps, list):
        return steps
    result = []
    for step in steps:
        if not isinstance(step, dict):
            result.append(step)
            continue
        raw_metadata = step.get("metadata")
        if isinstance(raw_metadata, str):
            try:
                raw_metadata = json.loads(raw_metadata)
            except (TypeError, ValueError):
                raw_metadata = None
        if not isinstance(raw_metadata, dict):
            result.append(step)
            continue
        approval = raw_metadata.get("approval")
        if not isinstance(approval, dict):
            result.append(step)
            continue
        result.append(
            {
                **step,
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

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    allowed = {"is_shared", "viewer_read_only"}
    if include_settings:
        allowed.update({"chat_profile", "chat_settings"})

    result = {
        **thread,
        "metadata": {key: value for key, value in metadata.items() if key in allowed},
    }
    if not include_settings and "steps" in thread:
        result["steps"] = _public_steps(thread.get("steps"))
    return cast(ThreadDict, result)
