# Security hardening: Azure AD hybrid OAuth

Status: **fixed** in `c41814d6` on `fix/azure-oauth-hardening`. This note keeps
the threat model, implementation details, and test plan for future review.
Already-ported details are in [upstream triage](upstream-triage.md).

## Scope

Only deployments using the `azure-ad-hybrid` provider are affected. Apps using
Google, GitHub, Keycloak, generic OIDC, or password auth are not exposed.

| Item                                                   | State                           |
| ------------------------------------------------------ | ------------------------------- |
| #3075 websocket session-ID path traversal              | Fixed in `55c1d069`             |
| #3048 Azure AD missing-token 500                       | Fixed in `55c1d069`             |
| #2825 OAuth state cookie path                          | Fixed in `55c1d069`             |
| **#3070 Azure hybrid state + `id_token` verification** | Fixed in `c41814d6`             |
| #3053 thread metadata leak                             | Open — separate, lower severity |

Upstream references:

- PR: <https://github.com/Chainlit/chainlit/pull/3070>
- Tests: <https://github.com/Chainlit/chainlit/pull/3071>
- Report: CVSS 9.3, CWE-347 (improper signature verification)

## Implementation summary

The fix in `c41814d6`:

- Requires and validates the OAuth `state` form field in the Azure hybrid
  callback.
- Generates a fresh nonce per login in `oauth_login`, sends it in the
  authorization request, and stores it in a path-scoped `oauth_nonce` cookie.
- Verifies the `id_token` against the tenant JWKS with `jwt.decode`
  (`RS256`, `aud`, `exp`, required `iss`/`nonce`), validates the issuer, and
  compares the nonce in constant time.
- Runs the JWKS key lookup through `asyncio.to_thread` and caches the
  `PyJWKClient`.
- Adds `pyjwt[crypto]` as a base dependency.
- Adds 10 backend tests, including valid and invalid locally-signed RS256
  tokens (wrong signature, audience, issuer, and nonce).
- Full suite: 846 passed; mypy, lint, format, and `uv lock --check` clean.

## The vulnerability

`POST /auth/oauth/azure-ad-hybrid/callback` accepts `code` and `id_token`
directly from a browser form (`response_mode=form_post`) and:

1. Never reads or validates the OAuth `state` form field.
2. Never verifies the `id_token` signature, `aud`, `exp`, or `nonce`.
3. Passes the browser-supplied `id_token` to the app's `oauth_callback`.

Anyone can post a forged, unsigned (`alg=none`) token and have Chainlit mint a
session for the identity in that token.

The provider also generates its `nonce` once at import time
(`AzureADHybridOAuthProvider.__init__`), so every login in the process lifetime
reuses the same nonce. That provides no replay protection.

## What the upstream patch does

- Adds `uses_per_login_nonce = True` to the Azure hybrid provider and removes
  the static nonce.
- Generates a fresh nonce in `oauth_login`, adds it to `authorize_params`, and
  stores it in an `oauth_nonce` cookie.
- Adds `state` to the form callback signature and validates it against the
  existing `oauth_state` cookie.
- Verifies the `id_token` with `jwt.PyJWKClient` + `jwt.decode`, checks the
  nonce claim with `hmac.compare_digest`, then calls `oauth_callback`.
- Clears the nonce cookie on success.

## Gaps in the upstream patch

Do not cherry-pick it unchanged.

1. **Missing crypto dependency.** `PyJWKClient` and RS256 require
   `cryptography`; it is not a base dependency today. Add `pyjwt[crypto]` to
   `backend/pyproject.toml` and relock. This adds `cryptography` to the base
   install.
2. **No `iss` validation**, despite the PR description. Add issuer validation:
   - Single tenant (tenant is a concrete ID, or
     `OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT` is set): pin
     `issuer=https://login.microsoftonline.com/{tenant}/v2.0`.
   - Multi-tenant (`common`/`organizations`): tokens carry a tenant-specific
     issuer, so reject anything that does not match
     `https://login.microsoftonline.com/{tenant-id}/v2.0`.
3. **Blocking I/O in async code.** `PyJWKClient.get_signing_key_from_jwt()`
   performs synchronous HTTP. Calling it directly in the FastAPI handler blocks
   the event loop. Cache the client per JWKS URL and run key lookup with
   `await asyncio.to_thread(...)`.
4. **Cookie path.** `set_oauth_nonce_cookie` and `clear_oauth_nonce_cookie` must
   use `path=_cookie_path`; the upstream patch omits it. The state cookie
   already has the path (our `55c1d069`).
5. **Weak tests.** #3071 mocks `_verify_azure_hf_id_token`, so a broken
   `jwt.decode` call would still pass. Add at least one real RS256 test with a
   locally generated key and a mocked JWKS client.
6. **Redirect helper.** Our `_get_oauth_redirect_error()` has no `status_code`
   argument; either add one (default 307, pass 303) or raise the existing
   error redirect as-is.

## Implementation checklist

### 1. Dependency

`backend/pyproject.toml`:

```toml
"pyjwt[crypto]>=2.13.0,<3.0.0",
```

Then `uv lock` from the repo root. Verify `uv lock --check`.

### 2. Nonce cookie helpers

`backend/chainlit/auth/cookie.py`:

```python
_nonce_cookie_name = "oauth_nonce"


def set_oauth_nonce_cookie(response: Response, token: str):
    response.set_cookie(
        _nonce_cookie_name,
        token,
        httponly=True,
        samesite=_cookie_samesite,
        secure=_cookie_secure,
        max_age=_state_cookie_lifetime,
        path=_cookie_path,
    )


def get_oauth_nonce_cookie(request: Request) -> Optional[str]:
    return request.cookies.get(_nonce_cookie_name)


def clear_oauth_nonce_cookie(response: Response):
    response.delete_cookie(_nonce_cookie_name, path=_cookie_path)
```

### 3. Provider

`backend/chainlit/oauth_providers.py`:

- Add `uses_per_login_nonce: bool = False` to `OAuthProvider`.
- Set `uses_per_login_nonce = True` on `AzureADHybridOAuthProvider`.
- Delete the static nonce from `authorize_params`; explain in a comment that
  the nonce is generated per login in `oauth_login`.

### 4. Login flow

`backend/chainlit/server.py`, `oauth_login`:

```python
random = random_secret(32)

authorize_params = dict(provider.authorize_params)
nonce = None
if getattr(provider, "uses_per_login_nonce", False):
    nonce = random_secret(16)
    authorize_params["nonce"] = nonce

params = urllib.parse.urlencode(
    {
        "client_id": provider.client_id,
        "redirect_uri": f"{get_user_facing_url(request.url)}/callback",
        "state": random,
        **authorize_params,
    }
)
response = RedirectResponse(url=f"{provider.authorize_url}?{params}")
set_oauth_state_cookie(response, random)
if nonce is not None:
    set_oauth_nonce_cookie(response, nonce)
return response
```

### 5. Verification helper

Add a cached JWKS client and an async verifier:

```python
import hmac
from functools import lru_cache


def _azure_hf_jwks_url() -> str:
    tenant = os.environ.get("OAUTH_AZURE_AD_HYBRID_TENANT_ID", "common")
    authority = os.environ.get(
        "OAUTH_AZURE_AD_HYBRID_AUTHORITY",
        "https://login.microsoftonline.com",
    )
    return f"{authority.rstrip('/')}/{tenant}/discovery/v2.0/keys"


@lru_cache(maxsize=4)
def _azure_hf_jwks_client(url: str):
    import jwt

    return jwt.PyJWKClient(url, cache_keys=True)


async def _verify_azure_hf_id_token(
    provider, id_token: str, expected_nonce: Optional[str]
) -> dict:
    import jwt

    if not expected_nonce:
        raise ValueError("missing nonce cookie")

    client = _azure_hf_jwks_client(_azure_hf_jwks_url())
    signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, id_token)

    decode_kwargs = {
        "algorithms": ["RS256"],
        "audience": provider.client_id,
    }
    if issuer := _azure_hf_issuer():
        decode_kwargs["issuer"] = issuer

    decoded = jwt.decode(id_token, signing_key.key, **decode_kwargs)

    token_nonce = decoded.get("nonce")
    if not token_nonce or not hmac.compare_digest(str(token_nonce), expected_nonce):
        raise ValueError("id_token nonce does not match")

    return decoded
```

`_azure_hf_issuer()` returns the pinned issuer only when the tenant is a
concrete ID; for `common`/`organizations`, validate the issuer pattern
`https://login.microsoftonline.com/<tenant-id>/v2.0` after decode (or use the
`tid` claim to build it).

Note: `jwt.decode` itself is CPU-only after the key is fetched; keeping it in
the event loop is fine.

### 6. Callback

`oauth_azure_hf_callback`:

- Add `state: Annotated[Optional[str], Form()] = None`.
- Reject missing state or missing `id_token` with
  `_get_oauth_redirect_error(request, "oauthSignin", status_code=303)`.
- Call `validate_oauth_state_cookie(request, state)`; reject on error.
- `await _verify_azure_hf_id_token(provider, id_token, get_oauth_nonce_cookie(request))`;
  reject on error.
- Only then exchange the code, fetch user info, and call `oauth_callback`.
- Clear both state and nonce cookies on success.

The upstream patch passes `status_code=303`; our helper needs an optional
argument for that.

## Tests

Add to `backend/tests/test_server.py` and/or
`backend/tests/test_oauth_providers.py`:

1. Missing `state` → 303 `error=oauthSignin`, callback not awaited.
2. Missing `id_token` → 303, callback not awaited.
3. Forged/unsigned `id_token` → 303, no auth cookie.
4. Nonce mismatch → 303.
5. Valid locally-signed RS256 token:
   - Generate an RSA key with `cryptography`.
   - Build claims: `aud`, `iss`, `exp`, `nonce`, `upn`.
   - Mock `_azure_hf_jwks_client` to return the public key.
   - Assert `oauth_callback` receives the claims and login succeeds.
6. Wrong audience/issuer/expired token → rejected.
7. `oauth_login` adds a fresh `nonce` to the authorize URL and sets the
   `oauth_nonce` cookie with the configured path.
8. Existing Azure token tests still pass (missing `access_token` → 400).

## Verification

```bash
uv sync --all-extras
uv run pytest -q backend/tests/test_server.py -k azure
uv run pytest -q backend/tests/test_oauth_providers.py -k azure
uv run pytest -q backend/tests
uv run scripts/type_check.py
uv run scripts/lint.py backend/chainlit backend/tests
uv run scripts/format.py --check
uv lock --check
```

## Rollout notes

- No configuration change is required for existing `azure-ad-hybrid` users.
- Users with a login in flight during deploy may need to retry once because the
  old nonce/state cookies no longer match.
- Multi-tenant (`common`) needs relaxed issuer validation; do not pin a single
  tenant or valid logins from other tenants will fail.
- Keep the `id_token` forwarding to `oauth_callback` unchanged after
  verification; apps may rely on its claims.

## Effort

~1.5–2 hours including tests and the dependency relock. The code path is small;
the care is in issuer handling, avoiding a blocking JWKS fetch, and proving the
cryptographic path with a real key in tests.
