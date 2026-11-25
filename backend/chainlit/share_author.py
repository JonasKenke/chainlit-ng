"""Resolve the public author label stored with shared prompts and skills."""

import inspect
import re
import unicodedata
from typing import Optional

from chainlit.config import config
from chainlit.logger import logger
from chainlit.user import User

_MAX_AUTHOR_LABEL_LENGTH = 200
_EMAIL_LIKE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _sanitize_author_label(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        " " if character.isspace() else character
        for character in normalized
        if character.isspace() or not unicodedata.category(character).startswith("C")
    )
    normalized = " ".join(normalized.split())
    return normalized[:_MAX_AUTHOR_LABEL_LENGTH].rstrip() or None


async def resolve_shared_author_label(user: Optional[User]) -> Optional[str]:
    """Return a safe, public label using only server-side authenticated data."""
    callback = config.code.shared_author_label
    if callback is not None:
        try:
            result: object = callback(user)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            logger.warning(
                "Shared author label callback failed (%s)", type(exc).__name__
            )
            return None
        return _sanitize_author_label(result)

    if user is None:
        return None

    metadata = user.metadata if isinstance(user.metadata, dict) else {}
    for candidate in (
        user.display_name,
        metadata.get("email"),
        metadata.get("preferred_username"),
        metadata.get("name"),
    ):
        label = _sanitize_author_label(candidate)
        if label is not None:
            return label

    identifier = _sanitize_author_label(user.identifier)
    if identifier is not None and _EMAIL_LIKE.fullmatch(identifier):
        return identifier
    return None
