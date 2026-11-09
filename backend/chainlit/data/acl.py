"""Single thread-read decision point and the owner-only write wrapper.

Every single-thread read (HTTP or socket) calls `authorize_thread_read`. The
result is an enum, not a message, so each route keeps its own status mapping.
Anything that is not an explicit allow is a denial.
"""

import json
from enum import Enum
from typing import TYPE_CHECKING, Optional

from fastapi import HTTPException

from chainlit.config import config
from chainlit.data import get_data_layer
from chainlit.logger import logger
from chainlit.types import ThreadDict, ThreadOwner
from chainlit.user import PersistedUser, User

if TYPE_CHECKING:
    from chainlit.data.base import BaseDataLayer

MAX_THREAD_GRANTS_PER_THREAD = 100


class ThreadAccess(str, Enum):
    MISSING = "missing"  # no row
    OWNERLESS = "ownerless"  # row exists, but neither owner field is set
    NONE = "none"  # row exists, caller is neither owner, public, nor granted
    OWNER = "owner"
    SHARED = "shared"  # named read grant
    PUBLIC = "public"  # anonymous is_shared link or an allowing callback


def _is_thread_owner(owner: Optional[ThreadOwner], user: Optional[User]) -> bool:
    """One namespace per row: a persisted id wins over the display identifier."""
    if owner is None or user is None:
        return False
    if owner.user_id is not None:
        user_id = getattr(user, "id", None)
        return user_id is not None and str(owner.user_id) == str(user_id)
    if owner.identifier is not None:
        return owner.identifier == user.identifier
    return False


def _thread_owner_from(thread: ThreadDict) -> ThreadOwner:
    """Typed owner of an already-fetched row; both fields None means ownerless."""
    return ThreadOwner(
        user_id=thread.get("userId"), identifier=thread.get("userIdentifier")
    )


def _thread_metadata(thread: ThreadDict) -> dict:
    metadata = (thread.get("metadata") if isinstance(thread, dict) else {}) or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata


async def authorize_thread_read(
    thread_id: str,
    user: Optional[User],
    *,
    thread: Optional[ThreadDict] = None,
    allow_public_share: bool = False,
    data_layer: Optional["BaseDataLayer"] = None,
) -> ThreadAccess:
    """Decide whether `user` may read `thread_id`, fail-closed.

    Args:
        thread_id: Thread to decide on.
        user: Caller, or None for an anonymous share viewer.
        thread: The fetched row when the caller already has it.
        allow_public_share: Only the share route passes True.
        data_layer: Layer to use; resolved from the app when omitted.
    """
    if data_layer is None:
        data_layer = get_data_layer()
    if not data_layer:
        return ThreadAccess.NONE

    # The typed owner predicate is part of the opt-in feature; with the flag
    # off the owner comparison stays legacy so the off path is unchanged.
    strict = bool(config.features.thread_grants)
    ownerless = False

    # Today's share route has no owner check; adding one there is part of the
    # opt-in feature, so with the flag off an unshared thread stays 404 for
    # its owner.
    if allow_public_share and not strict:
        pass
    elif strict:
        owner: Optional[ThreadOwner] = None
        if thread is not None:
            owner = _thread_owner_from(thread)
        else:
            try:
                owner = await data_layer.get_thread_owner(thread_id)
            except Exception:
                if not allow_public_share:
                    raise
                logger.warning(
                    "Thread owner lookup failed (thread=%s, outcome=deny)", thread_id
                )
                owner = None
        if owner is None:
            return ThreadAccess.MISSING
        if owner.user_id is None and owner.identifier is None:
            ownerless = True
        elif _is_thread_owner(owner, user):
            return ThreadAccess.OWNER
    else:
        if thread is not None:
            author = thread.get("userIdentifier")
            if author is None:
                ownerless = True
        else:
            try:
                author = await data_layer.get_thread_author(thread_id)
            except ValueError:
                # Built-in data layers raise ValueError for an unknown thread.
                author = None
            except Exception:
                if not allow_public_share:
                    raise
                logger.warning(
                    "Thread owner lookup failed (thread=%s, outcome=deny)", thread_id
                )
                author = None
            if not author:
                return ThreadAccess.MISSING
        if user is not None and author == user.identifier:
            return ThreadAccess.OWNER

    if allow_public_share and thread is not None:
        user_can_view = False
        if getattr(config.code, "on_shared_thread_view", None):
            try:
                user_can_view = await config.code.on_shared_thread_view(thread, user)
            except Exception:
                user_can_view = False
        if user_can_view or bool(_thread_metadata(thread).get("is_shared")):
            return ThreadAccess.PUBLIC

    if ownerless:
        return ThreadAccess.OWNERLESS
    if not strict or not getattr(data_layer, "thread_grants_supported", False):
        return ThreadAccess.NONE
    if (
        not isinstance(user, PersistedUser)
        or not getattr(user, "id", None)
        or not getattr(user, "createdAt", None)
    ):
        return ThreadAccess.NONE

    try:
        grant = await data_layer.get_thread_grant(thread_id, user.id)
    except Exception:
        logger.warning(
            "Thread grant lookup failed (thread=%s, outcome=deny)", thread_id
        )
        return ThreadAccess.NONE

    if (
        grant is not None
        and grant.role == "read"
        and grant.grantee_user_id == user.id
        and grant.grantee_user_created_at == user.createdAt
    ):
        return ThreadAccess.SHARED
    return ThreadAccess.NONE


async def is_thread_author(username: str, thread_id: str):
    """Owner check for write routes: 404 for no row, 401 for another user."""
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data layer not initialized")

    if not config.features.thread_grants:
        try:
            thread_author = await data_layer.get_thread_author(thread_id)
        except ValueError:
            # Built-in data layers raise ValueError for an unknown thread.
            thread_author = None

        if not thread_author:
            raise HTTPException(status_code=404, detail="Thread not found")
        if thread_author != username:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return True

    try:
        owner = await data_layer.get_thread_owner(thread_id)
    except ValueError:
        owner = None

    if owner is None or (owner.user_id is None and owner.identifier is None):
        raise HTTPException(status_code=404, detail="Thread not found")

    if owner.user_id is not None:
        # Quiet lookup: this path must not log identifiers at INFO.
        try:
            user = await data_layer.get_user_for_acl(username)
        except Exception:
            user = None
        if user is None or getattr(user, "id", None) is None:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not _is_thread_owner(owner, user):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return True

    if owner.identifier != username:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True


async def _require_owner_or_404(thread_id: str, user: Optional[User]) -> None:
    """Owner gate for grant management: 404 for both missing and foreign rows."""
    access = await authorize_thread_read(thread_id, user)
    if access != ThreadAccess.OWNER:
        raise HTTPException(status_code=404, detail="Thread not found")
