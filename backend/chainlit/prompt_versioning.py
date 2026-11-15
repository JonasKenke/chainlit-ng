"""Prompt/skill versioning primitives shared by the data layer and the API.

This module owns the immutability contract: a version row is a snapshot of the
content-addressed prompt body. The head row and the version rows are written in
one transaction by the SQLAlchemy layer, and callers use :func:`fingerprint` to
decide whether an edit deserves a new version.
"""

import difflib
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# A prompt body is capped so one row cannot exhaust a data layer.
MAX_PROMPT_CONTENT_BYTES = 64 * 1024
# Explicit history is bounded; an append at the cap fails instead of evicting.
MAX_PROMPT_VERSIONS = 200
# Diff responses are bounded independently of the content caps.
MAX_DIFF_ROWS = 2000
MAX_DIFF_BYTES = 256 * 1024

FINGERPRINT_PREFIX = "sha256:"


class PromptVersionError(Exception):
    """Base class for typed versioning failures the API maps to HTTP errors."""

    code = "prompt_version_error"


class PromptVersionConflict(PromptVersionError):
    """The requested version cannot be written: it is stale or duplicated."""

    code = "version_conflict"


class PromptVersionLimitReached(PromptVersionError):
    """The prompt already holds the maximum number of stored versions."""

    code = "version_limit_reached"


class PromptContentTooLarge(PromptVersionError):
    """The prompt body exceeds :data:`MAX_PROMPT_CONTENT_BYTES`."""

    code = "content_too_large"


def prompt_kind(prompt: Mapping[str, Any]) -> str:
    kind = prompt.get("kind")
    return kind if isinstance(kind, str) and kind else "prompt"


def fingerprint(prompt: Mapping[str, Any]) -> str:
    """Return a stable hash of the fields that constitute one version.

    Prompts are identified by title and content. Skills also include their
    description because it is part of the rendered skill payload.
    """
    kind = prompt_kind(prompt)
    parts: List[Any] = [kind, prompt.get("title") or "", prompt.get("content") or ""]
    if kind == "skill":
        parts.insert(2, prompt.get("description") or "")
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_PREFIX}{digest}"


def fingerprint_from_values(
    kind: str, title: str, content: str, description: str = ""
) -> str:
    return fingerprint(
        {
            "kind": kind,
            "title": title,
            "content": content,
            "description": description,
        }
    )


def enforce_content_limit(content: str) -> None:
    """Raise :class:`PromptContentTooLarge` before any write happens."""
    if len(content.encode("utf-8")) > MAX_PROMPT_CONTENT_BYTES:
        raise PromptContentTooLarge(
            f"Prompt content must be at most {MAX_PROMPT_CONTENT_BYTES} bytes"
        )


def _row(
    op: str,
    old_line: Optional[int],
    new_line: Optional[int],
    old_text: Optional[str],
    new_text: Optional[str],
) -> Dict[str, Any]:
    return {
        "op": op,
        "oldLine": old_line,
        "newLine": new_line,
        "oldText": old_text,
        "newText": new_text,
    }


def build_diff(
    old_text: str,
    new_text: str,
    max_rows: int = MAX_DIFF_ROWS,
    max_bytes: int = MAX_DIFF_BYTES,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return ``(rows, truncated)`` for a line diff, bounded by rows and bytes.

    Rows are presentation-neutral (no HTML, no markup): the client decides how
    to render them. Line numbers are 1-based and ``None`` when a side has no
    corresponding line.
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    rows: List[Dict[str, Any]] = []
    used_bytes = 0
    truncated = False

    def append(row: Dict[str, Any]) -> bool:
        nonlocal used_bytes, truncated
        row_bytes = len((row["oldText"] or "").encode("utf-8")) + len(
            (row["newText"] or "").encode("utf-8")
        )
        if len(rows) >= max_rows or used_bytes + row_bytes > max_bytes:
            truncated = True
            return False
        rows.append(row)
        used_bytes += row_bytes
        return True

    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                if not append(
                    _row(
                        "equal",
                        i1 + offset + 1,
                        j1 + offset + 1,
                        old_lines[i1 + offset],
                        new_lines[j1 + offset],
                    )
                ):
                    return rows, truncated
        elif tag == "delete":
            for index in range(i1, i2):
                if not append(_row("delete", index + 1, None, old_lines[index], None)):
                    return rows, truncated
        elif tag == "insert":
            for index in range(j1, j2):
                if not append(_row("insert", None, index + 1, None, new_lines[index])):
                    return rows, truncated
        else:  # replace
            span = max(i2 - i1, j2 - j1)
            for offset in range(span):
                old_index = i1 + offset
                new_index = j1 + offset
                old_value = old_lines[old_index] if old_index < i2 else None
                new_value = new_lines[new_index] if new_index < j2 else None
                if old_value is not None and new_value is not None:
                    op = "replace"
                elif old_value is not None:
                    op = "delete"
                else:
                    op = "insert"
                if not append(
                    _row(
                        op,
                        old_index + 1 if old_value is not None else None,
                        new_index + 1 if new_value is not None else None,
                        old_value,
                        new_value,
                    )
                ):
                    return rows, truncated

    return rows, truncated


def public_version(version: Dict[str, Any], include_author: bool) -> Dict[str, Any]:
    """Project a version row for the API, dropping author identity on shares."""
    projected = {
        "id": version["id"],
        "promptId": version["promptId"],
        "kind": version.get("kind", "prompt"),
        "version": version["version"],
        "title": version["title"],
        "description": version.get("description") or "",
        "contentHash": version["contentHash"],
        "createdAt": version["createdAt"],
        "sourceVersionId": version.get("sourceVersionId"),
    }
    if include_author:
        projected["createdBy"] = version.get("createdBy")
    if "content" in version:
        projected["content"] = version["content"]
    return projected


def latest_head(versions: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the highest version from an unordered or ordered sequence."""
    if not versions:
        return None
    return max(versions, key=lambda item: int(item["version"]))


def public_versions(
    prompt: Mapping[str, Any], versions: Sequence[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Return the versions a public share may expose.

    Sharing records the version that was current when the link was created.
    That version and every later one are public; older drafts stay private.
    Legacy shares without a baseline keep their historical all-versions
    behavior.
    """
    base = prompt.get("sharedVersion")
    if base is None:
        return list(versions)
    try:
        minimum = int(base)
    except (TypeError, ValueError):
        return list(versions)
    return [
        version for version in versions if int(version.get("version") or 0) >= minimum
    ]
