import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt as pyjwt

from chainlit.config import config
from chainlit.logger import logger
from chainlit.user import User, redact_sensitive_metadata

JWT_ISSUER = os.environ.get("CHAINLIT_JWT_ISSUER", "chainlit")
JWT_AUDIENCE = os.environ.get("CHAINLIT_JWT_AUDIENCE", "chainlit")


def get_jwt_secret() -> Optional[str]:
    # An empty variable is not a secret; treat it as missing so startup fails
    # closed instead of raising an AssertionError on the first login.
    return os.environ.get("CHAINLIT_AUTH_SECRET") or None


def create_jwt(data: User) -> str:
    safe_user = redact_sensitive_metadata(data) or data
    to_encode: Dict[str, Any] = safe_user.to_dict()
    to_encode.update(
        {
            "exp": datetime.now(timezone.utc)
            + timedelta(seconds=config.project.user_session_timeout),
            "iat": datetime.now(timezone.utc),  # Add issued at time
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
        }
    )

    secret = get_jwt_secret()
    assert secret
    encoded_jwt = pyjwt.encode(to_encode, secret, algorithm="HS256")
    return encoded_jwt


def decode_jwt(token: str) -> User:
    secret = get_jwt_secret()
    assert secret

    decoded = pyjwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        # Validate iss/aud manually so tokens minted before these claims were
        # added keep working. A present but foreign claim is still rejected.
        options={
            "verify_signature": True,
            "verify_aud": False,
            "require": ["exp"],
        },
    )
    if "iss" in decoded and decoded["iss"] != JWT_ISSUER:
        raise pyjwt.InvalidIssuerError("Invalid issuer")
    if "aud" in decoded:
        audiences = decoded["aud"]
        if isinstance(audiences, str):
            audiences = [audiences]
        if not isinstance(audiences, list) or JWT_AUDIENCE not in audiences:
            raise pyjwt.InvalidAudienceError("Invalid audience")
    del decoded["exp"]
    user = redact_sensitive_metadata(User(**decoded))
    assert user is not None
    return user


def _revocation_key(token: str) -> str:
    return f"revoked-jwt:{hashlib.sha256(token.encode()).hexdigest()}"


async def revoke_token(token: str) -> None:
    """Best-effort revocation until the normal session lifetime expires."""
    from chainlit.state_store import get_state_store

    try:
        await get_state_store().set(
            _revocation_key(token),
            b"1",
            ttl=config.project.user_session_timeout,
        )
    except Exception:
        logger.exception("Unable to persist JWT revocation")


async def is_token_revoked(token: str) -> bool:
    from chainlit.state_store import get_state_store

    try:
        return await get_state_store().exists(_revocation_key(token))
    except Exception:
        # Revocation is an enhancement, not a reason to take login down when
        # the shared state store is unavailable.
        logger.exception("Unable to read JWT revocation state")
        return False
