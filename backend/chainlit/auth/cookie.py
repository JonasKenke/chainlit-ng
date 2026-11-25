import hmac
import os
from typing import Literal, Optional, cast

from fastapi import Request, Response
from fastapi.exceptions import HTTPException
from fastapi.security.base import SecurityBase
from fastapi.security.utils import get_authorization_scheme_param
from starlette.status import HTTP_401_UNAUTHORIZED

from chainlit.config import config

""" Module level cookie settings. """
_cookie_samesite = cast(
    Literal["lax", "strict", "none"],
    os.environ.get("CHAINLIT_COOKIE_SAMESITE", "lax"),
)

assert _cookie_samesite in [
    "lax",
    "strict",
    "none",
], (
    "Invalid value for CHAINLIT_COOKIE_SAMESITE. Must be one of 'lax', 'strict' or 'none'."
)


def _get_cookie_secure() -> bool:
    """Resolve the Secure flag; SameSite=None always requires it.

    CHAINLIT_COOKIE_SECURE=true forces Secure on HTTPS deployments. The flag is
    opt-in because Secure cookies are dropped by browsers on plain HTTP. Leave
    it unset for HTTP deployments; CHAINLIT_COOKIE_SAMESITE=none still forces
    Secure on its own because browsers reject SameSite=None without it.
    """
    raw = os.environ.get("CHAINLIT_COOKIE_SECURE")
    if raw is None:
        return _cookie_samesite == "none"

    value = raw.strip().lower()
    if value in ("true", "1"):
        return True
    if value in ("false", "0"):
        return _cookie_samesite == "none"

    raise ValueError(
        "Invalid value for CHAINLIT_COOKIE_SECURE. Must be 'true', '1', 'false' or '0'."
    )


_cookie_secure = _get_cookie_secure()
if _cookie_root_path := os.environ.get("CHAINLIT_ROOT_PATH", None):
    _cookie_path = _cookie_root_path
else:
    _cookie_path = os.environ.get("CHAINLIT_AUTH_COOKIE_PATH", "/")
_state_cookie_lifetime = int(
    os.environ.get("CHAINLIT_STATE_COOKIE_LIFETIME", str(3 * 60))
)
_auth_cookie_name = os.environ.get("CHAINLIT_AUTH_COOKIE_NAME", "access_token")
# Up to 2.12.0 the auth cookies were always set with this path, whatever the
# configured one. Where the configured path differs, browsers keep sending such
# a cookie next to the current one, and Starlette keeps the last of two cookies
# with the same name, which is the one with the shorter path. So it is removed
# whenever the auth cookies are set or cleared.
_legacy_cookie_path = "/"
_state_cookie_name = "oauth_state"
_nonce_cookie_name = "oauth_nonce"


class OAuth2PasswordBearerWithCookie(SecurityBase):
    """
    OAuth2 password flow with cookie support with fallback to bearer token.
    """

    def __init__(
        self,
        tokenUrl: str,
        scheme_name: Optional[str] = None,
        auto_error: bool = True,
    ):
        self.tokenUrl = tokenUrl
        self.scheme_name = scheme_name or self.__class__.__name__
        self.auto_error = auto_error

    async def __call__(self, request: Request) -> Optional[str]:
        # First try to get the token from the cookie
        token = get_token_from_cookies(request.cookies)

        # If no cookie, try the Authorization header as fallback
        if not token:
            # TODO: Only bother to check if cookie auth is explicitly disabled.
            authorization = request.headers.get("Authorization")
            if authorization:
                scheme, token = get_authorization_scheme_param(authorization)
                if scheme.lower() != "bearer":
                    if self.auto_error:
                        raise HTTPException(
                            status_code=HTTP_401_UNAUTHORIZED,
                            detail="Invalid authentication credentials",
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                    else:
                        return None
            else:
                if self.auto_error:
                    raise HTTPException(
                        status_code=HTTP_401_UNAUTHORIZED,
                        detail="Not authenticated",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                else:
                    return None

        return token


def _get_chunked_cookie(cookies: dict[str, str], name: str) -> Optional[str]:
    # Gather all auth_chunk_i cookies, sorted by their index
    chunk_parts = []

    i = 0
    while True:
        cookie_key = f"{_auth_cookie_name}_{i}"
        if cookie_key not in cookies:
            break

        chunk_parts.append(cookies[cookie_key])
        i += 1

    joined = "".join(chunk_parts)

    return joined if joined != "" else None


def get_token_from_cookies(cookies: dict[str, str]) -> Optional[str]:
    """
    Read all chunk cookies and reconstruct the token
    """

    # Default/unchunked cookies
    if value := cookies.get(_auth_cookie_name):
        return value

    return _get_chunked_cookie(cookies, _auth_cookie_name)


def _secure_for_request(request: Request) -> bool:
    """Mark the auth cookie Secure whenever the app is served over HTTPS.

    CHAINLIT_COOKIE_SECURE still forces or disables the flag. Auto-detection
    covers the common direct-HTTPS and CHAINLIT_URL deployments without
    breaking plain-HTTP development, where a Secure cookie would be dropped.
    """
    if _cookie_secure:
        return True
    if request.url.scheme == "https":
        return True
    chainlit_url = os.environ.get("CHAINLIT_URL")
    return bool(chainlit_url and chainlit_url.lower().startswith("https://"))


def set_auth_cookie(request: Request, response: Response, token: str):
    """
    Helper function to set the authentication cookie with secure parameters
    and remove any leftover chunks from a previously larger token.
    """

    _chunk_size = 3000
    secure = _secure_for_request(request)

    existing_cookies = {
        k for k in request.cookies.keys() if k.startswith(_auth_cookie_name)
    }

    if len(token) > _chunk_size:
        chunks = [token[i : i + _chunk_size] for i in range(0, len(token), _chunk_size)]

        for i, chunk in enumerate(chunks):
            k = f"{_auth_cookie_name}_{i}"

            response.set_cookie(
                key=k,
                value=chunk,
                httponly=True,
                secure=secure,
                samesite=_cookie_samesite,
                max_age=config.project.user_session_timeout,
                path=_cookie_path,
            )

            existing_cookies.discard(k)
    else:
        # Default (shorter cookies)
        response.set_cookie(
            key=_auth_cookie_name,
            value=token,
            httponly=True,
            secure=secure,
            samesite=_cookie_samesite,
            max_age=config.project.user_session_timeout,
            path=_cookie_path,
        )

        existing_cookies.discard(_auth_cookie_name)

    # Delete remaining prior cookies/cookie chunks
    for k in existing_cookies:
        response.delete_cookie(
            key=k, path=_cookie_path, secure=secure, samesite=_cookie_samesite
        )

    _delete_legacy_auth_cookies(request, response)


def clear_auth_cookie(request: Request, response: Response):
    """
    Helper function to clear the authentication cookie
    """

    secure = _secure_for_request(request)
    existing_cookies = {
        k for k in request.cookies.keys() if k.startswith(_auth_cookie_name)
    }

    for k in existing_cookies:
        response.delete_cookie(
            key=k, path=_cookie_path, secure=secure, samesite=_cookie_samesite
        )

    _delete_legacy_auth_cookies(request, response)


def _delete_legacy_auth_cookies(request: Request, response: Response):
    """Delete auth cookies left at the legacy path by a version before 3.0.0."""

    if _cookie_path == _legacy_cookie_path:
        return

    secure = _secure_for_request(request)
    for k in request.cookies.keys():
        if k.startswith(_auth_cookie_name):
            response.delete_cookie(
                key=k,
                path=_legacy_cookie_path,
                secure=secure,
                samesite=_cookie_samesite,
            )


def set_oauth_state_cookie(
    response: Response, token: str, request: Optional[Request] = None
):
    secure = _secure_for_request(request) if request is not None else _cookie_secure
    response.set_cookie(
        _state_cookie_name,
        token,
        httponly=True,
        samesite=_cookie_samesite,
        secure=secure,
        max_age=_state_cookie_lifetime,
        path=_cookie_path,
    )


def validate_oauth_state_cookie(request: Request, state: str):
    """Check the state from the oauth provider against the browser cookie."""

    oauth_state = request.cookies.get(_state_cookie_name)

    if oauth_state is None or not hmac.compare_digest(oauth_state, state):
        raise Exception("oauth state does not correspond")


def set_oauth_nonce_cookie(
    response: Response, token: str, request: Optional[Request] = None
):
    """Bind an OIDC nonce to the browser for an in-flight login."""
    secure = _secure_for_request(request) if request is not None else _cookie_secure
    response.set_cookie(
        _nonce_cookie_name,
        token,
        httponly=True,
        samesite=_cookie_samesite,
        secure=secure,
        max_age=_state_cookie_lifetime,
        path=_cookie_path,
    )


def get_oauth_nonce_cookie(request: Request) -> Optional[str]:
    """Read the OIDC nonce bound to the browser's in-flight login."""
    return request.cookies.get(_nonce_cookie_name)


def clear_oauth_state_cookie(response: Response):
    """Oauth complete, delete state token."""
    response.delete_cookie(
        _state_cookie_name,
        path=_cookie_path,
        secure=_cookie_secure,
        samesite=_cookie_samesite,
    )


def clear_oauth_nonce_cookie(response: Response):
    """Oauth complete, delete nonce token."""
    response.delete_cookie(
        _nonce_cookie_name,
        path=_cookie_path,
        secure=_cookie_secure,
        samesite=_cookie_samesite,
    )


def clear_oauth_cookies(response: Response):
    """Clear both cookies that belong to an in-flight login."""
    clear_oauth_state_cookie(response)
    clear_oauth_nonce_cookie(response)
