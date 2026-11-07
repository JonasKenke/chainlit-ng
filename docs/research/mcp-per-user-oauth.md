# Per-user OAuth for HTTP MCP connections (#3013)

Status: **design, revision 3 — accepted by the Security Specialist (KRE-36).**
This note contains no production code.

Review pointer: branch `design/mcp-per-user-oauth` (revision 1 = `ed1364c0`,
revision 2 = `073c5ced`). There is **no reviewable OneDev PR** for this note —
review the branch/commit, not a PR number. Revision 1 was referenced from KRE-19
as "PR #5"; OneDev PR #5 is a merged Renovate PR, so that reference was a process
error and has been removed.

Revision 1 was reviewed by the Security Specialist and returned **`block`**
(KRE-19 comment `b31b9063`): **H1** (bypassable authorization-server allowlist)
plus **M1–M8**, with **L2/L4/L7** folded in and **L1/L3/L5/L6/L8** accepted by the
CEO as documented residuals. Revision 2 was reviewed again and returned
**`block`** (KRE-19 comment `b9addd6e`): the server-origin exception was still
path-based, so an MCP URL whose path was an AS discovery path (or a path-bearing
PRM `authorization_servers[0]`) could still make the user-chosen server act as
the authorization server; M8's per-key generation could be reused after LRU
eviction; and several low/AC gaps remained. Revision 3 closes both H1 bypasses
by scoping the server-origin exception to **marked MCP transport requests** and
the **exact PRM discovery URLs**, makes generations store-wide monotonic, and
folds the remaining findings into the sections and ticket-ready slice ACs
(Appendix A maps every finding → section → slice AC). The recommended
architecture is unchanged — Option A, memory-only tokens, SDK-owned
PKCE/state/refresh.

Section numbering follows revision 1: **§8** is files/blast radius and **§9**
holds the ticket-ready slices (KRE-36's text calls the slice section "§8").

Sources: upstream PR [#3013](https://github.com/Chainlit/chainlit/pull/3013)
(part of issue #2197), `docs/research/upstream-triage.md` L92, this fork's MCP
integration, `mcp` 2.3.0 in `backend/.venv`.

## 0. What changed in revisions 2–3

Revision 2 (`073c5ced`) folded H1.1/H1.3/H1.4/H1.6, M1–M7 and L2/L4/L7 into the
design. Revision 3 (this commit) fixes H1's remaining bypass (H1.2 was path-based),
M8's generation reuse, and the low/AC gaps from the second review. The rows below
carry the revision-3 disposition; Appendix A maps every finding → section → slice
AC.

| Finding            | Disposition in this revision                                                                                                                                                                                                                                                |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| H1.1 empty = off   | `oauth_authorization_origins` is a **hard gate**: empty ⇒ `400` for every `useOAuth: true`, always (§5.2 step 2d). No "same origin" exemption exists.                                                                                                                       |
| H1.2 server origin | The server origin is **never** an AS allowlist entry. A request-role marker allows it only for **MCP transport** requests and for **exact PRM discovery URLs** (GET, no query); AS metadata/DCR/token/refresh on that origin need an explicit entry (§5.1, §5.2 steps 4–5). |
| H1.3 second opt-in | New default-off `[features.mcp.user_oauth]` with `enabled` + start rate limit and pending cap (§5.4, §5.6).                                                                                                                                                                 |
| H1.4 normalization | `normalize_origin()` compares `(scheme, host, port)` exactly, rejects userinfo/non-http(s)/plain-http-off-loopback (§5.1). DNS rebinding stays a stated residual (§6, R13).                                                                                                 |
| H1.5 guard tests   | Enumerated in S4a-3…S4a-7 and S4b-1/S4b-2/S4b-11, including both second-review bypass variants (§9).                                                                                                                                                                        |
| H1.6 T5/R2         | T5 and R2 rewritten: the guard closes OAuth-path steering only; the plain MCP path keeps its pre-existing SSRF surface (§6, §10).                                                                                                                                           |
| M1 deadlines       | `asyncio.wait_for` on both waits, monotonic TTL, a timer task that actually wakes the waiter, caps, cancel paths, exactly one event (§5.6, S2).                                                                                                                             |
| M2 growth          | Per-user/global entry caps with LRU eviction, DCR info deleted on abandon/failure when no token was written, client-info TTL, start rate limit (§5.3, §5.6, S1).                                                                                                            |
| M3 headers         | With `useOAuth`, `authorization`/`proxy-authorization`/`cookie` are rejected `400` (§5.2 step 2g, S4b-5).                                                                                                                                                                   |
| M4 key             | Userinfo rejected for OAuth MCP URLs, never silently dropped; SDK divergence stated and pinned (§5.1, S1-3/S1-4).                                                                                                                                                           |
| M5 SSE             | CEO decision: `useOAuth` on SSE is `400` in v1; alternative cost stated (§5.2 step 2c, S4b-4).                                                                                                                                                                              |
| M6 `DELETE /mcp`   | `McpSession.oauth_server_key` set at connect, `forget_server()` + pending cancel on delete (§5.2 step 11, S4b-8).                                                                                                                                                           |
| M7 logout          | Identity resolved **optionally**; cookie always cleared; `on_logout` always runs; pending flows cancelled (§5.5, S3-4/S3-5).                                                                                                                                                |
| M8 refresh         | Store-wide monotonic generation: refresh failure deletes only the token it read, even after LRU eviction and re-create (§5.3, S1-7).                                                                                                                                        |
| L2                 | Callback page headers, fixed details in the callback, `202` body and `mcp_connection_failed`, origin-only application logging; SDK logger residual stated (§5.2 steps 9–10, §6 T11, S3-3).                                                                                  |
| L4                 | DCR `client_secret` treated as a secret in assets, logs and `forget_*` (§5.3, S1-6).                                                                                                                                                                                        |
| L7                 | `SameSite` prerequisite (`lax`, or `none`+`Secure`; `strict` unsupported); `CHAINLIT_URL` required and `https` outside loopback (§5.4, S3-7, S4b-6).                                                                                                                        |
| M2 abandon AC      | An abandoned/failed flow before a token is written leaves no tokens and no `client_info` for its key (§5.3, S4b-7).                                                                                                                                                         |
| M6 pending key     | `DELETE /mcp` uses the pending entry's `server_key` when no `McpSession` exists yet (§5.2 step 11, S4b-8).                                                                                                                                                                  |
| Config entries     | `oauth_authorization_origins` entries with a path/query/fragment/userinfo are rejected at load, not silently reduced to their origin (§5.4, S4a-1).                                                                                                                         |
| Authorization URL  | `redirect_handler` rejects non-`http(s)`, userinfo and plain `http` off loopback for the authorization URL (§5.2 step 6, S4b-12).                                                                                                                                           |
| L9                 | Logout CSRF under `SameSite=None` documented as an accepted residual/follow-up (§5.5, §10).                                                                                                                                                                                 |

## Recommendation in one paragraph

Port the _idea_ of #3013, not its diff. Keep the MCP SDK's `OAuthClientProvider`
as the only OAuth implementation — it already owns discovery, dynamic client
registration, PKCE (`S256`), `state` generation and token refresh — and supply
only what it cannot know: which Chainlit user a flow belongs to, and how to get
the authorization URL in front of that user's browser. Tokens live in a
process-wide **in-memory** store keyed by `(user identifier, canonical server
URL)`. The callback route is registered on the existing `router`, so it inherits
`CHAINLIT_ROOT_PATH` for free. `POST /mcp` answers `202` as soon as the redirect
has been handed to the browser and reports the final outcome over the existing
socket, so no HTTP request is held open across a human sign-in. OAuth is
per-connection (`useOAuth`), needs **two** independent opt-ins (an operator's
authorization-server allowlist and a user-OAuth switch), and every request the
OAuth flow makes is checked against a normalized-origin allowlist that the
user-supplied MCP server origin cannot extend. The server origin is reachable only
by requests marked as the MCP transport and by the exact protected-resource
metadata discovery URLs; every authorization-server request must be on an
explicitly listed origin.

## 1. Problem and constraints

HTTP MCP connections in this fork authenticate with static headers declared in
the connection form (`ConnectStreamableHttpMCPRequest.headers`). There is no way
for an individual user to sign in to an MCP server, so every deployment either
shares one credential or has none.

Constraints that shape the design:

| #   | Constraint                                                                          | Consequence                                                                                                                                   |
| --- | ----------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| C1  | Chainlit is one multi-user server, not a desktop client                             | Every token has an owner; no process-global "the user".                                                                                       |
| C2  | The SDK's `TokenStorage` interface takes no arguments                               | Isolation must be provided by handing the SDK a pre-scoped view, not by the SDK keying anything.                                              |
| C3  | The authorization redirect leaves Chainlit and returns on a route every user shares | `state` is the only correlation handle, and it must be bound to the authenticated caller.                                                     |
| C4  | Deployments sit behind proxies and at sub-paths (`CHAINLIT_ROOT_PATH`)              | The callback URL and the route must be derived from the same source as the login callback; no request may be held open across the round-trip. |
| C5  | Authentication is optional in Chainlit                                              | OAuth is refused when there is no authenticated user.                                                                                         |
| C6  | All changes must be backward compatible (`AGENTS.md`)                               | New behaviour only behind new opt-in inputs; existing request/response shapes untouched.                                                      |
| C7  | The fork has no persistence layer guarantee                                         | Tokens stay in memory; a restart forces re-authorization.                                                                                     |
| C8  | The fork is on `mcp>=2.0.0,<3.0.0`, venv 2.3.0                                      | The SDK surface differs from what PR #3013 was written against (see §3).                                                                      |
| C9  | `POST /mcp` takes **arbitrary user URLs**; this fork has no `allowed_urls`          | The OAuth flow starts from an attacker-chosen origin, so the AS allowlist must be a hard gate and must not trust that origin (H1).            |
| C10 | The store is per process                                                            | Single-worker deployments only; multi-worker needs a shared store (L3, documented).                                                           |

## 2. What upstream #3013 does, and why it cannot be cherry-picked

PR #3013 is 17 files, +2603/−159. Roughly half of it is test code (1436 lines).
It is written against an upstream MCP integration this fork does not have:

| #3013 depends on                                                                                                                                                            | In this fork (`main`)                                                                                                                                                  |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Named developer servers `[[features.mcp.servers]]`, `SseMcpServer` / `StreamableHttpMcpServer` config models, `McpServerOAuth`, `oauth = { authorization_origins = [...] }` | **Absent.** `McpFeature` (`backend/chainlit/config.py:323-329`) has only `enabled` + per-transport toggles.                                                            |
| `features.mcp.user_servers` opt-in and `allowed_urls` allowlist                                                                                                             | **Absent.** `POST /mcp` accepts any user-supplied URL.                                                                                                                 |
| Destination pinning: `_destination_on_origin`, `McpDestinationError`, `make_mcp_http_client_factory`, `validate_mcp_url`, `validate_mcp_headers`                            | **Absent.** `backend/chainlit/mcp.py` is 99 lines: four Pydantic models plus `validate_mcp_command`.                                                                   |
| `stop_mcp_task`, `swap_mcp_session`, `_MCP_CONNECT_TIMEOUT_HTTP`, `_mcp_connects_awaiting_authorization`                                                                    | **Absent.** `backend/chainlit/server.py:2137` awaits `ready_event.wait()` with no timeout at all.                                                                      |
| Socket events `mcp_connected` / `mcp_connection_failed`; `isUserProvided`; `migrateStoredMcps`; `libs/react-client/src/utils/mcp.ts`; named-server UI                       | **Absent.** `IMcp` is 10 lines and `readStoredMcps()` (`state.ts:80`) is an unvalidated `JSON.parse`.                                                                  |
| Transport named `streamablehttp_client`, `auth=` accepted as a transport argument, `callback_handler` returns `(code, state)`                                               | **Different.** SDK 2.3.0 exposes `streamable_http_client(url, *, http_client, ...)` with no `auth` argument; `callback_handler` must return `AuthorizationCodeResult`. |
| Endpoint-level test harness for `/mcp` (`mcp_chainlit_app`, `mock_mcp_transport`, ~1400 lines)                                                                              | **Absent.** `backend/tests/test_mcp.py` (446 lines) only exercises the models and `validate_mcp_command`.                                                              |

So #3013 is a source of design decisions and a provider/store implementation to
adapt, not a patch to apply. Roughly 45% of the upstream diff (config + named
servers + allowlist + destination-check refactor of `mcp.py`) has no target in
this fork; ~55% (store, pending map, provider, callback route, `/mcp` wiring,
frontend) does.

## 3. What the MCP SDK already gives us (verified against 2.3.0)

| SDK surface                           | Fact we build on                                                                                                                                                                                                                                       |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `mcp.client.auth.OAuthClientProvider` | Is an `httpx2.Auth` (`mcp/shared/_httpx_utils.py:188`), so it is passed as `httpx2.AsyncClient(auth=provider)` — exactly one line in this fork's `connect_mcp`, which already builds that client explicitly.                                           |
| `TokenStorage` Protocol               | Only `get_tokens`/`set_tokens`/`get_client_info`/`set_client_info`. No user argument — this is why C2 exists.                                                                                                                                          |
| `_perform_authorization_code_grant`   | Mints `state = secrets.token_urlsafe(32)`, PKCE `S256`, calls `redirect_handler(authorization_url)`, awaits `callback_handler()`, then `secrets.compare_digest(result.state, state)`.                                                                  |
| `OAuthClientMetadata`                 | `redirect_uris` required; `application_type` defaults to `"native"` — a browser-based server must pass `"web"`.                                                                                                                                        |
| `_auth_flow`                          | On `401`/`insufficient_scope`: PRM discovery (`/.well-known/oauth-protected-resource`, SEP-985 fallbacks), then **`authorization_servers[0]` from that metadata** (`oauth2.py:594`), then AS metadata, DCR, redirect, code exchange, refresh.          |
| `RedirectAwareAuth.async_auth_flow`   | Already distinguishes the request being authenticated (`outgoing is request`) from the flow's own requests (`mcp/shared/_httpx_utils.py:206-222`). `MarkedOAuthAuth` uses the same identity split to attach the role marker; no SDK source is patched. |
| `_validate_resource_match`            | Rejects a PRM `resource` that is not a parent of the server URL (RFC 8707). Already on by default; we do not need to reimplement it.                                                                                                                   |
| `resource_url_from_server_url`        | Canonicalises scheme+netloc case and drops the fragment; **keeps** path, query, userinfo and an explicit default port.                                                                                                                                 |
| `_handle_refresh_response`            | On failure calls `context.clear_tokens()` (memory only) and returns `False`; it **never deletes the stored token**. `ChainlitOAuthProvider` overrides this one method to call `storage.delete_failed_token()`, or a dead token survives until restart. |
| `sse_client`                          | Accepts `auth=` (`sse.py:42`) but builds its **own** `httpx2` client through `httpx_client_factory` (`sse.py:67`), so `event_hooks` cannot be attached without a custom factory — the reason M5 is rejected rather than wired (§5.2).                  |

The SDK is therefore the security-critical machinery. The design does not
reimplement PKCE, state minting, discovery or refresh, and must not.

## 4. Options

### Option A — 202 + socket events (recommended)

`POST /mcp` starts the connection, waits at most until the SDK hands out the
authorization URL, then answers `202 {"status": "authorization_required", "url",
"mcp"}` and finishes the connection in a background task. The outcome arrives as
`mcp_connected` or `mcp_connection_failed` on the session's socket.

- (+) No HTTP request lives across the browser round-trip; works behind any
  proxy and with a 5-minute authorization window.
- (+) `DELETE /mcp` can cancel a pending flow, so "cancel while signing in"
  behaves.
- (−) Three new socket events and frontend state handling (`connecting` →
  signed in → `connected`/`failed`).
- (−) A page reload during sign-in loses the websocket session, so the flow is
  abandoned in the browser and only times out server-side.

### Option B — synchronous wait bounded by the authorization TTL

Keep today's request/response and let the handler wait for the code.

- (+) Two socket events fewer; the frontend change is one checkbox plus passing
  `useOAuth` through.
- (−) Holds a request open for up to `AUTHORIZATION_TTL`. The fork's uvicorn
  default behind a reverse proxy (nginx `proxy_read_timeout` 60 s, Cloud Run
  60 s idle) kills the connection while the server keeps waiting; the user sees
  a failed add but a session may be attached later. `APIBase.post` has no client
  timeout, so the browser also hangs.
- (−) A reload or closed tab cannot be distinguished from a slow sign-in.

### Option C — session-scoped tokens only (no per-user key)

Store tokens on the `WebsocketSession` (`session.mcp_sessions` already exists).

- (+) Smallest diff: no cross-session store, cleanup falls out of
  `WebsocketSession.delete()`.
- (−) **Fails C1.** Two browser tabs are two websocket sessions for the same
  user; a reconnect returns a new session and silently forgets the token,
  forcing re-authorization on every reload. Worse, if the store ever outlives
  the session the key is attacker-influenced (`payload.sessionId`), which is the
  cross-user leak this feature must not create.
- (−) Does not survive the 202 flow anyway (Option A needs the store to outlive
  the request), so the saving is illusory.

**Recommendation: A.** B is only viable if the deployment target is always a
direct uvicorn with no proxy; C trades the one property the feature exists for.

## 5. Recommended design

### 5.1 Modules

New `backend/chainlit/mcp_oauth.py`, mirroring upstream's name so a later
re-sync is cheap. It imports only stdlib + `mcp` + `httpx2` (no `chainlit.session`
import), so `session.py` and `server.py` can use it without cycles:

| Symbol                                    | Responsibility                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `normalize_origin(url) -> Origin`         | Returns `(scheme, host, port)` for comparison. Rejects non-`http(s)`, rejects userinfo, lowercases the host, strips one trailing dot, IDNA-encodes the host, maps default ports (`80`/`443`) to `None`, and rejects plain `http` outside loopback (`localhost`, `127.0.0.0/8`, `::1`). Never compares strings, so `allowed.example.evil.example` cannot pass.                                                                        |
| `canonical_server_key(url) -> str`        | Store key: the normalized origin plus path verbatim (empty path → `/`, trailing slash significant) plus query, fragment dropped. **Never drops userinfo silently** — see M4 below.                                                                                                                                                                                                                                                   |
| `McpOAuthTokenStore`                      | `Dict[(user_identifier, server_key)]` holding tokens, `client_info` and a **store-wide monotonic** `generation` counter; each entry carries the stamp of its last **token** write. Reachable only through `scoped()`, `forget_user()`, `forget_server()`, `discard_client_info()`, `delete_if_unchanged()`, `generation()`. Caps: `MAX_ENTRIES_PER_USER = 20`, `MAX_ENTRIES_TOTAL = 500`, LRU eviction, `_CLIENT_INFO_TTL = 3600 s`. |
| `ScopedTokenStorage`                      | Implements the SDK's `TokenStorage` Protocol, fixed to one `(user, server)` pair. Refuses an empty user identifier. Records the entry's store-wide generation when `get_tokens` loads it and when `set_tokens` writes; `delete_failed_token()` calls `delete_if_unchanged(key, recorded_generation)`.                                                                                                                                |
| `PendingAuthorizations`                   | `(state) -> (user, server_key, deadline, session_id, name, future, timer_task)`. `register`, `resolve(state, user, code)`, `abandon(state, reason)`, `cancel_session(session_id)`, `cancel_name(session_id, name)`, `cancel_user(user)`. Single-use, `AUTHORIZATION_TTL = 300 s` monotonic, per-entry timer task that **wakes** the parked waiter, `MAX_PENDING_PER_USER = 3`.                                                       |
| `OriginAllowlist` + `oauth_request_guard` | Parses `features.mcp.oauth_authorization_origins` into a `set[Origin]`; returns an httpx2 **request** event hook that enforces the request-role rule: a `transport`-marked request may reach only the server origin, an `oauth`-marked request only a listed origin or an exact PRM URL (§5.2 steps 4–5).                                                                                                                            |
| `MarkedOAuthAuth`                         | `httpx2.Auth` wrapper around the SDK provider; sets `requires_response_body = True` (the provider’s value) so the SDK can read PRM/token bodies. Marks the request passed into `async_auth_flow` as `transport` and every SDK-yielded request that is not that object as `oauth`, then delegates to the provider's own flow.                                                                                                         |
| `McpOAuthRateLimiter`                     | In-memory sliding window per `user.identifier` for flow starts (`max_starts_per_user_per_minute`, default 5); `429` when exceeded.                                                                                                                                                                                                                                                                                                   |
| `build_oauth_provider(...)`               | Builds a small `ChainlitOAuthProvider(OAuthClientProvider)` with `redirect_uris=[redirect_uri]`, `application_type="web"`, `grant_types=["authorization_code","refresh_token"]`, and the two handlers below; overrides `_handle_refresh_response` only to call `storage.delete_failed_token()` when the SDK returns `False`. Returns the provider wrapped in `MarkedOAuthAuth`.                                                      |

SDK adaptation required (the reason #3013's provider is not copy-pasteable):

- `callback_handler()` returns `AuthorizationCodeResult(code=..., state=...)`,
  **not** `(code, state)`. The `iss` member stays `None`; the SDK validates it
  only when the authorization server sends one.
- The provider is bound as `auth=` on the `httpx2.AsyncClient` that
  `connect_mcp` already creates — not as a transport argument. It is wrapped in
  `MarkedOAuthAuth`, which is also an `httpx2.Auth` and delegates to the
  provider's `async_auth_flow`.
- The SDK's `_auth_flow` yields the original transport request object at the
  start and again after a successful authorization (`oauth2.py:604`, `:797`),
  and constructs **new** request objects for PRM, AS metadata, DCR, token and
  refresh. The marker relies on that object identity, not on a URL path.
- `OAuthClientProvider._handle_refresh_response` clears only its in-memory
  token on failure (`oauth2.py:533`) and never calls `storage`;
  `ChainlitOAuthProvider` overrides that one method to call
  `storage.delete_failed_token()` when the SDK returns `False`. Refresh itself
  stays SDK-owned.
- `callback_handler()` is wrapped in `asyncio.wait_for(remaining_ttl)` so an
  abandoned browser tab cannot park the SDK forever (M1).
- `httpx2` 2.16.0 exposes `Request.extensions` and runs request event hooks for
  the requests an auth flow yields; the role marker and the guard rely on both
  (S4a-3 pins it).

**The origin allowlist (H1).** The guard is the only new network boundary. It
does not trust a URL path: the provider is wrapped so each request carries a
role marker, and the server-origin exception applies only to the **marked MCP
transport request** and to the **exact PRM discovery URLs**.

```text
# MarkedOAuthAuth.async_auth_flow (shape; responses are forwarded with asend)
request.extensions[OAUTH_ROLE] = "transport"
flow = provider.async_auth_flow(request)
outgoing = await flow.__anext__()
while True:
    if outgoing is not request:
        outgoing.extensions[OAUTH_ROLE] = "oauth"
    response = yield outgoing
    try:
        outgoing = await flow.asend(response)
    except StopAsyncIteration:
        return

# oauth_request_guard(request)
role = request.extensions.get(OAUTH_ROLE)
origin = normalize_origin(request.url)          # raises on userinfo / http-off-loopback

if role == "transport":
    if origin == server_origin: return          # the MCP client owns this request
    raise McpOAuthOriginError

if role == "oauth":
    if origin in allowed_origins: return        # AS metadata, DCR, token, refresh
    if (origin == server_origin
            and request.method == "GET"
            and not request.url.query
            and canonical_url(request.url) in prm_urls):
        return                                  # exact RFC 9728 discovery only
    raise McpOAuthOriginError

raise McpOAuthOriginError                       # unmarked: fail closed
```

where `prm_urls` is
`set(build_protected_resource_metadata_discovery_urls(None, canonical_server_url))`
plus each URL's single-trailing-slash variant (the SDK's `RedirectAwareAuth`
follows same-origin redirects for auth requests). `build_protected_resource_metadata_discovery_urls`
is the SDK's own public helper (`mcp/client/auth/utils.py:64`), so the set stays
in sync with the SDK's PRM construction. `canonical_server_url` is the normalized
origin plus the verbatim path (the same components as `canonical_server_key`).
The guard canonicalizes the request URL the same way (normalized origin +
the path as `httpx2` parsed it, query and fragment removed), so host case and an
explicit default port do not break the exact match.

Why this closes both second-review bypasses:

- **Variant A — the MCP URL path is an AS discovery path.** The transport request
  is allowed (it is the marked MCP request), but the PRM request is marked
  `oauth` and only exact PRM URLs are allowed. The AS metadata request to
  `/.well-known/oauth-authorization-server` (the first
  `build_oauth_authorization_server_metadata_discovery_urls` candidate,
  `utils.py:159`) is marked `oauth`, is not a PRM URL and its origin is not
  listed, so it is refused before any DCR/token request. The MCP URL path is
  also rejected at the gate when it contains `/.well-known/` (§5.2 step 2h), so
  the bypass cannot be set up in the first place.
- **Variant B — path-bearing PRM `authorization_servers[0]`.** The SDK's third
  AS metadata candidate is `{P}/.well-known/openid-configuration`
  (`utils.py:183-186`). It could equal a PRM URL only if the MCP path
  (`server_path`) ended in `/.well-known/openid-configuration`; the gate rejects
  any `/.well-known/` segment in the MCP path. The other candidates prefix
  `authorization-server`/`openid-configuration`, never
  `oauth-protected-resource`. No `authorization_servers[0]` value can therefore
  turn an AS metadata request into an exact PRM URL.
- **DCR/token/refresh to the MCP endpoint path.** These are marked `oauth` and
  are POSTs; the PRM exception is GET-only. The transport marker is attached to
  the request object passed into `async_auth_flow`; the SDK constructs new
  objects for every OAuth request, so an OAuth request cannot inherit it.
- **Custom `resource_metadata` from `WWW-Authenticate`.** The SDK tries that URL
  first (`build_protected_resource_metadata_discovery_urls`); the guard refuses
  it unless it is one of the two exact PRM URLs. The SDK's `_auth_flow` logs and
  re-raises the guard error rather than falling back, so a server that publishes
  PRM only at a custom location fails closed (R10). Listing the server origin as
  an AS origin would allow AS metadata there too; that is the explicit
  same-origin case, not a default.
- The server origin is a **transport and PRM-discovery** exception, not an
  allowlist entry. An authorization server that happens to live on the MCP
  server's origin reaches `/.well-known/oauth-authorization-server`, `/register`
  and `/token` — none of which is an exact PRM URL — so it is **refused unless
  the operator lists that origin explicitly** (H1.2).
- The guard runs per request, so a redirect to a non-permitted origin is also
  refused (the SDK's `RedirectAwareAuth` already only follows same-origin
  redirects for auth requests; this is belt-and-braces). A transport redirect is
  still a `transport` request and may only reach the server origin.
- Only the OAuth client gets the hook. Plain connections are untouched.
- Rejected origins are logged as `scheme://host:port` only — never path or query
  (L2).

H1's two reported bypasses are closed; the server origin grants no
authorization-server capability. Remaining network residuals are DNS rebinding
of a listed AS (§6, R13), the pre-existing plain-path SSRF (§6, R2), and the
custom-PRM availability limitation above (R10). None of them lets a
user-chosen server act as an authorization server.

**M4 — key derivation.** `canonical_server_key` must not merge two tenants.
Revision 1 dropped userinfo, so `https://alice:pw@host/mcp` and
`https://bob:pw@host/mcp` shared a key while the SDK's
`resource_url_from_server_url` keeps userinfo (different audience). Revision 2
**rejects userinfo in an MCP URL when `useOAuth` is set** (`400`) and documents
that the plain path keeps today's behaviour, because rejecting it for every MCP
URL is a backward-compatibility change this ticket does not need. The divergence
from `resource_url_from_server_url` (it keeps an explicit default port, we drop
it) is documented in the docstring and pinned by a test: the SDK value is only
used as the RFC 8707 `resource` parameter, so two spellings of one server share
one token — harmless — while two tenants (different path/query) never do.

### 5.2 Flow

1. Frontend posts `/mcp` with `clientType: "streamable-http"`, `name`, `url`,
   `useOAuth: true`.
2. `connect_mcp` gates in this order, **before any network call**:
   a. MCP not enabled → `400` (existing).
   b. Authentication not configured or no `current_user` → `401`.
   c. `useOAuth` on `stdio` → `400`; `useOAuth` on `clientType: "sse"` → `400` with a fixed detail ("OAuth is not supported for SSE MCP connections; use streamable HTTP") and no `sse_client` call (M5, §5.2 note below).
   d. `features.mcp.oauth_authorization_origins` **empty → `400`**, always; a hard gate with no exemption, not even when the AS origin equals the server origin (H1.1).
   e. `features.mcp.user_oauth.enabled` false → `400` naming the field (second opt-in, H1.3).
   f. `CHAINLIT_URL` unset or `http` outside loopback → `400` ("OAuth requires an https CHAINLIT_URL", L7), so the redirect URI is never derived from the `Host` header and the callback code/state do not travel in clear text; `http://localhost`/`http://127.0.0.1` is allowed for a local dev loop only.
   g. `authorization`, `proxy-authorization` or `cookie` in `headers` (case-insensitive) → `400` (M3): `useOAuth` and a static credential are mutually exclusive, otherwise a shared bearer would authenticate the first request and the SDK would never start OAuth — a silent downgrade rather than an error.
   h. MCP URL must be `http(s)`, must not contain userinfo and must be `https` outside loopback → `400` otherwise (M4, H1.4). Its path must not contain `/.well-known/` (case-insensitive, raw or percent-decoded) → `400` (H1 variant A).
   i. Start rate limit or per-user pending cap exceeded → `429` (H1.3, M1).

   Cost of the M5 alternative: a custom `httpx_client_factory` that passes `auth=provider` **and** the request hook (~20 lines), a second guarded client path, and endpoint tests for a transport nobody asked to authenticate per user — deferred until there is demand.

3. `server_key = canonical_server_key(url)`. If the user already holds
   `MAX_ENTRIES_PER_USER` entries, the LRU entry with no in-flight flow is
   evicted first (M2), so a flow can always proceed.
4. The background task builds the transport on the `httpx2.AsyncClient` that
   `connect_mcp` already creates, adding `auth=MarkedOAuthAuth(provider)` and a
   request hook `event_hooks={"request": [guard]}`. `initialize()` reaches the
   server, gets `401`, and the SDK starts PRM discovery. The transport request
   is marked `transport`; the PRM request is marked `oauth` and is allowed only
   because it is one of the exact PRM URLs for `server_key`. The PRM document
   names `authorization_servers[0]`.
5. **Every SDK-constructed request is marked `oauth` and checked by the
   guard**: AS metadata, DCR, token and refresh requests pass only if their
   normalized origin is in `oauth_authorization_origins`. A PRM document naming
   any other origin — the server's own origin included — fails with
   `McpOAuthOriginError` and **no request is sent** to it (H1.2): the server
   origin grants no AS capability by path, and the only server-origin exception
   for an `oauth` request is an exact PRM URL, `GET`, no query. The connection
   fails with a fixed detail; the blocked origin goes to the log only as
   `scheme://host:port`.
6. `redirect_handler(url)`: reject a non-`http(s)` URL, a URL with userinfo,
   and a plain `http` URL outside loopback (same rule as the MCP URL); log only
   the origin. Then register `(state → user, key, deadline, session_id, name)`;
   emit `mcp_authorization_required {name, url}`; set
   `authorization_requested`. The advertised authorization endpoint is not
   required to be on a listed AS origin (L1 residual: the frontend shows its
   host).
7. `connect_mcp` waits at most `AUTHORIZATION_URL_TIMEOUT = 10 s` for that event
   (`asyncio.wait_for`). On success it returns `202` with
   `{"status": "authorization_required", "url", "mcp"}` and schedules
   `_finish_after_authorization()` keyed by `(session.id, name)`, cancelling any
   previous attempt for that key.
8. The user signs in; the authorization server redirects to
   `GET {root_path}/mcp/oauth/callback?code=…&state=…`.
9. The callback route requires `current_user` (a missing or expired JWT is a
   plain `401`, never a crash — see the SameSite prerequisite in §5.4), then
   `resolve(state, current_user.identifier, code)`. Unknown/expired → `400`;
   another user's state → `403` **without consuming it**; success → the pending
   future resolves, the SDK compares `state` itself, exchanges the code with
   PKCE, and `storage.set_tokens` persists under `(user, server)`, stamping the
   entry with a new store-wide generation. The response is a plain HTML page with
   `Cache-Control: no-store` and `Referrer-Policy: no-referrer`; every non-success
   maps to a **fixed** detail from a closed set (never `str(exc)`, which for a
   state mismatch contains both states) (L2).
10. The background task attaches the session (or fails), emits exactly one of
    `mcp_connected {mcp}` / `mcp_connection_failed {name, detail}` where
    `detail` is a fixed code (never `str(exc)`), clears the pending entry, and
    cancels its timer task. On abandon/timeout before a token was written, it
    also calls `token_store.discard_client_info(key)`, which removes the DCR
    registration only when the entry has no tokens (M2); a concurrent
    successful flow's token and client info survive.
11. `DELETE /mcp` first looks up the pending entry for `(session, name)`: if it
    exists, it uses that entry's `server_key` (there is no `McpSession` yet) and
    cancels the flow. If a `McpSession` exists, it uses
    `mcp_session.oauth_server_key`, the canonical key stored at connect (M6).
    It then calls `token_store.forget_server(user, key)` and runs the usual
    disconnect. The callback route then answers `400` instead of attaching a
    session the user already cancelled.
12. Websocket session end cancels that session's pending flows
    (`cancel_session`) and closes attached MCP sessions as today; tokens stay,
    because the user may open a second tab.

### 5.3 Where tokens live

In memory, in one store per process. No database, no disk, no `localStorage`.
Rationale: (a) Chainlit's data layer is optional and this feature must work
without one; (b) encrypted-at-rest for OAuth refresh tokens is a separate problem
with its own key-management story; (c) a restart forcing re-authorization is an
acceptable and safe default for credentials this sensitive. Persistence, if ever
wanted, slots in behind `ScopedTokenStorage` without touching the SDK or the
routes.

Assets (all of them are secrets, L4): the per-user access token, the refresh
token, the authorization code and PKCE verifier in flight, the SDK `state`, and
the DCR **`client_secret`** inside `OAuthClientInformationFull`. None of them are
logged, returned in an HTTP response, put in a socket payload or written to disk;
`forget_user`/`forget_server` clear tokens **and** `client_info`, so a DCR secret
does not outlive the token it was minted for.

Growth is bounded (M2): `MAX_ENTRIES_PER_USER = 20` (LRU eviction at flow start),
`MAX_ENTRIES_TOTAL = 500` (LRU eviction in `set_tokens`/`set_client_info`), and
entries that hold a DCR registration but no tokens expire after
`_CLIENT_INFO_TTL = 3600 s`.
A flow that is abandoned or fails calls `discard_client_info(key)`, which removes
the `client_info` it created only when the entry has no tokens, so a user cannot
fill a shared authorization server's registration table by starting flows while
a concurrent successful flow's token and client info stay intact. The test reads
the store (S4b-7).

The key's first component is `user.identifier` (`auth/__init__.py`), so it must
be stable and never reassigned to another human: a custom auth layer that reuses
an identifier would hand one person another's token. That requirement is
documented (L5, S6-3); purging a deleted user's entries is a follow-up because
this fork has no user-deletion hook the store could listen to.

Refresh (M8): two tabs may hold two providers for the same `(user, server)`.
`ScopedTokenStorage.get_tokens` records the entry's **store-wide monotonic
token generation**; every `set_tokens` increments the counter and stamps the
entry, while `set_client_info` does not change the token generation (so an
unrelated client-info write cannot make a dead token survive). If the SDK's
refresh fails, `ChainlitOAuthProvider._handle_refresh_response` calls
`storage.delete_failed_token()`, which invokes `delete_if_unchanged(key,
generation)` and deletes only if the entry still carries the generation read at
`get_tokens`. A token written by a concurrent, successful refresh (new
generation) survives, so a loser's `invalid_grant` cannot delete the winner's
rotated token. Because the counter is
store-wide and never resets, LRU eviction followed by a re-create of the same
key cannot reuse an old value; the previous per-key counter could restart at the
same small number and let a stale failure delete the new token (second-review
M8). The per-key lock alternative was rejected because it serializes whole
connects for one server and blocks the second tab behind the first human
sign-in.

### 5.4 Configuration

Two new fields, both defaulting to "off". OAuth requires **both**:

```toml
[features.mcp]
enabled = false
# Origins of authorization servers users may be sent to for MCP OAuth.
# HARD GATE: empty disables OAuth entirely (400 for any useOAuth request).
# The MCP server's own origin is NOT an entry here: it is allowed only for the
# MCP transport request and exact protected-resource discovery. An authorization
# server on the MCP server's origin must be listed explicitly.
# Each entry must be a bare origin (no path/query/fragment/userinfo).
oauth_authorization_origins = []

[features.mcp.user_oauth]
# Second, independent opt-in for OAuth against user-supplied MCP servers.
# POST /mcp accepts arbitrary URLs in this fork (no allowed_urls), so an
# authenticated user chooses the server and therefore the authorization server.
enabled = false
max_starts_per_user_per_minute = 5
max_pending_per_user = 3
```

- Entries are bare origins: `http(s)`, no path, no query, no fragment, no
  userinfo. A config entry with a path/query/fragment/userinfo is **rejected at
  load**, not silently reduced to its origin: `https://as.example/foo` and
  `https://as.example?x=1` are errors, because accepting them would make the
  allowlist broader than it reads. Entries are validated through the same
  `normalize_origin()`: lowercase host, one trailing dot stripped, IDNA/punycode
  compared, default port dropped, `https` required outside loopback. A plain
  `http` "internal" authorization server is refused — the code and tokens would
  travel in clear text; use a TLS proxy.
- Comparison is exact `(scheme, host, port)`; `https://allowed.example.evil.example`
  and `https://allowed.example@evil.example` are not matches.
- Prerequisites (L7, documented in S6 and checked in S4b-6):
  - `CHAINLIT_URL` must be set when OAuth is used and must be `https` outside
    loopback (`http://localhost`/`http://127.0.0.1` is allowed for a local dev
    loop only), so the redirect URI is not derived from the `Host` header and
    the callback code and state do not travel in clear text.
  - `CHAINLIT_COOKIE_SAMESITE` must be `lax` (default) or `none` + `Secure`.
    `strict` is unsupported for this feature: the JWT cookie is not sent on the
    cross-site callback and the route answers `401`. With `none`, a cross-site
    `POST /logout` is possible because the route has no CSRF check today; see
    the accepted residual L9 in §5.5.
  - Single process only: a callback that lands on another worker cannot see the
    pending flow or the token (L3).

No per-server `oauth` table: this fork has no named servers. When named servers
land, the same store and provider take a config-derived flag and allowlist
instead of a request field — a wiring change, not a redesign.

### 5.5 Logout and expiry

- `POST /logout` (`server.py:603-611`) resolves identity **optionally**: it
  attempts the same decode `get_current_user` uses and swallows a missing,
  expired or invalid token. It must not become a required `current_user`
  dependency — that would turn an expired-JWT logout into a `401` **before**
  `clear_auth_cookie`, leaving both the cookie and the tokens in place (M7).
- When identity resolves: `token_store.forget_user(identifier)` and
  `pending.cancel_user(identifier)`, **before** `clear_auth_cookie`, so a
  custom `on_logout` cannot skip it.
- `clear_auth_cookie` and `config.code.on_logout` always run, including when
  identity did not resolve and when neither store call was reached. A raising
  `on_logout` still leaves the cookie cleared and the tokens gone.
- When authentication is disabled or the cookie is absent, both store calls are
  no-ops and logout behaves exactly as today.
- Live tabs keep their in-memory tokens (the SDK provider holds them for the
  life of the connection) until they disconnect; logout is a server-side
  credential drop, not a broadcast revocation.
- With `CHAINLIT_COOKIE_SAMESITE=none`, a cross-site `POST /logout` can force a
  sign-out and drop that user's MCP tokens; the existing route has no CSRF or
  `Origin` check. Accepted residual **L9**: the impact is sign-out and
  credential re-entry, not token theft; `lax` is the default and blocks the
  cross-site POST. A follow-up adds an `Origin`/`Sec-Fetch-Site` check.
- Access-token expiry: the SDK refreshes transparently when a refresh token
  exists and overwrites the store on success.
- Refresh failure or an abandoned flow: our wrapper deletes the entry before
  re-raising, because the SDK clears only its in-memory copy (§3), and only if
  the entry is unchanged (§5.3).

### 5.6 Limits, deadlines and cleanup (M1, M2, H1.3)

Every value except the two under `[features.mcp.user_oauth]` (`max_starts_per_user_per_minute`,
`max_pending_per_user`) is a module constant in `mcp_oauth.py`, overridable in
tests (no extra config surface — nobody asked for it):

| Constant                         | Value      | Applies to                                                                                           |
| -------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------- |
| `AUTHORIZATION_URL_TIMEOUT`      | `10.0 s`   | `connect_mcp` waiting for `redirect_handler` to produce the URL; timeout ⇒ `400` + failure event     |
| `AUTHORIZATION_TTL`              | `300.0 s`  | deadline registered per flow (monotonic); also the bound on `callback_handler`                       |
| `SWEEP_INTERVAL`                 | `30.0 s`   | one periodic task, started on first pending entry, that expires entries and client-info-only entries |
| `MAX_PENDING_PER_USER`           | `3` (cfg)  | in-flight flows; the 4th start is `429` (a per-user cap covers every session of that user)           |
| `MAX_STARTS_PER_USER_PER_MINUTE` | `5` (cfg)  | flow starts; sliding window, `429`                                                                   |
| `MAX_ENTRIES_PER_USER`           | `20`       | store entries; LRU eviction at flow start                                                            |
| `MAX_ENTRIES_TOTAL`              | `500`      | store entries; LRU eviction on write                                                                 |
| `_CLIENT_INFO_TTL`               | `3600.0 s` | entries that hold a DCR registration but no token                                                    |

Wakeups are real, not lazy: each pending entry owns an `asyncio.Task` that sleeps
until its deadline and resolves the future with `McpAuthorizationTimeout`, so a
parked `callback_handler` cannot outlive the TTL. A lazy sweep alone cannot wake
a waiter, which is what revision 1 left underspecified. The timer task is
cancelled on every terminal outcome (resolve, abandon, session close, `DELETE
/mcp`, user logout), and `PendingAuthorizations` asserts no entry, future or task
remains after the tests. Cancellation itself waits at most the existing
`McpSession` close budget (`session.py:_CLOSE_TIMEOUT`, 10 s).

Exactly one terminal event per connect attempt: the background task owns a
`done` flag and emits `mcp_connected` or `mcp_connection_failed` once; a
superseded attempt for the same `(session, name)` is cancelled before the new
one starts.

## 6. Threat model

Assets: per-user OAuth access and refresh tokens for external MCP servers; the
DCR `client_secret`; the authorization code and PKCE verifier in flight; the
identity of the user a token belongs to.

Trust boundaries: browser ↔ Chainlit (JWT cookie); Chainlit ↔ MCP server (a
**user-chosen** origin); Chainlit ↔ authorization server (an operator-chosen
origin); user ↔ user inside one Chainlit process.

| #   | Threat                                                                                                                                                                                                                  | Mitigation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Verified by                                                                                                                                                                                                                                                               |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| T1  | User B's connection presents user A's token                                                                                                                                                                             | Store key is `(user.identifier, canonical server key)`; the SDK only ever sees a `ScopedTokenStorage`; no API returns a token.                                                                                                                                                                                                                                                                                                                                                                                               | Unit: same server, two users → different entries; `scoped("")` raises.                                                                                                                                                                                                    |
| T2  | A token is presented to a different server (confused deputy, e.g. same host, different tenant path)                                                                                                                     | Canonical key keeps path **and** query verbatim; client registration is stored under the same key; the SDK's RFC 8707 `resource` check stays on. RFC 8707 parent matching does not prove the AS scoped the token to the exact server, so we do not claim it does.                                                                                                                                                                                                                                                            | Unit: `/mcp` vs `/mcp/` vs `/mcp?tenant=b` are three keys.                                                                                                                                                                                                                |
| T3  | An attacker completes someone else's flow (state hijack)                                                                                                                                                                | State is SDK-generated (`token_urlsafe(32)`), single-use, 300 s TTL, and bound to the authenticated caller; a mismatched user gets `403` and the entry is not consumed. The SDK re-compares the state itself.                                                                                                                                                                                                                                                                                                                | Unit + endpoint: resolve as wrong user → `403`, owner still succeeds; second resolve → `400`.                                                                                                                                                                             |
| T4  | Authorization code interception / injection                                                                                                                                                                             | PKCE `S256` and verifier held only in the provider's memory (SDK); the code arrives only on the authenticated callback route; no code, verifier, token or DCR secret is logged or returned. Access-log exposure of the callback query is a documented residual (single-use code + PKCE).                                                                                                                                                                                                                                     | Review + grep-level test: response bodies and log calls never contain `code=`/tokens/client secrets.                                                                                                                                                                      |
| T5  | SSRF: the user-supplied MCP server steers discovery/DCR/token calls at internal addresses (`169.254.169.254`, `localhost`, an intranet host)                                                                            | The guard allows the server origin only for `transport`-marked MCP requests and for exact PRM discovery URLs (`GET`, no query; the SDK's two well-known URLs). **Every** AS metadata/DCR/token/refresh request must be on an explicitly listed `(scheme, host, port)`; a same-origin AS is not implied. https-only outside loopback; userinfo rejected; redirects to other origins refused.                                                                                                                                  | Unit: same-origin AS unlisted → no request sent; `https://allowed.example@evil.example`, `https://allowed.example.evil.example`, `http://169.254.169.254`, `http://10.0.0.5` refused; default-port/trailing-dot/IDN equivalence accepted; transport vs oauth role matrix. |
| T6  | Token exposure at rest or on the wire to the browser                                                                                                                                                                    | In-memory only; never in socket payloads (only `name` + authorization URL), never in `localStorage` (the frontend drops `authorizationUrl` when rehydrating), never in `/mcp` responses.                                                                                                                                                                                                                                                                                                                                     | Frontend unit: stored MCP entry with `authorizationUrl` loses it on rehydrate.                                                                                                                                                                                            |
| T7  | OAuth used where there is no per-user identity                                                                                                                                                                          | `401` when authentication is not configured or the caller is unauthenticated; refused before any transport is opened.                                                                                                                                                                                                                                                                                                                                                                                                        | Endpoint: unauthenticated `useOAuth` → `401`, no transport call.                                                                                                                                                                                                          |
| T8  | Resource exhaustion: many in-flight flows, orphaned tasks                                                                                                                                                               | `MAX_PENDING_PER_USER = 3`, `MAX_STARTS_PER_USER_PER_MINUTE = 5`, monotonic `AUTHORIZATION_TTL`, a timer task that wakes the waiter, one background task per `(session, name)`, cancel on session close and `DELETE /mcp`, exactly one terminal event.                                                                                                                                                                                                                                                                       | Endpoint: two connects for one name leave one task; abandon/timeout/session-close each leave no task, future or entry; disconnect during sign-in attaches nothing.                                                                                                        |
| T9  | DCR / discovery abuse against a shared authorization server                                                                                                                                                             | Two independent opt-ins must be on; only listed origins are reachable; start rate limit and per-user pending cap bound attempts; each attempt creates at most one registration per `(user, server)` and deletes it on abandon when no token was written. Residual: any authenticated user can trigger a registration on a listed AS once the operator opts in.                                                                                                                                                               | Review; S4b-2/S4b-3/S4b-7 tests.                                                                                                                                                                                                                                          |
| T10 | Open redirect / `javascript:` URL via the sign-in link                                                                                                                                                                  | The frontend opens the URL only if `http(s):`, with `noopener,noreferrer`, and only from a click (no automatic `window.open`).                                                                                                                                                                                                                                                                                                                                                                                               | Frontend unit: `javascript:`/`data:` refused.                                                                                                                                                                                                                             |
| T11 | Information leak through error paths                                                                                                                                                                                    | OAuth failures return a fixed detail from a closed set in the callback, the `202` error body and `mcp_connection_failed.detail`; the authorization URL, token endpoint response, `state` and `str(OAuthFlowError)` never appear in a client-visible error; our application logs carry at most the normalized origin. The SDK's own `mcp.client.auth.oauth2` logger can log the exception text (state mismatch contains both states; token error contains the response body) — accepted residual **L2b**, not client-visible. | Endpoint: callback body, 202 body and socket detail contain no URL/state; state-mismatch body contains neither state.                                                                                                                                                     |
| T12 | Replay of a completed callback                                                                                                                                                                                          | Entry deleted on resolve; the SDK rejects a mismatched state.                                                                                                                                                                                                                                                                                                                                                                                                                                                                | Unit: replay → `400`.                                                                                                                                                                                                                                                     |
| T13 | Two tabs sign in to the same server concurrently                                                                                                                                                                        | Independent states; the later attachment wins through the existing name-keyed `mcp_sessions` eviction; the earlier session's token remains valid and reusable, which is not a leak (same user, same server).                                                                                                                                                                                                                                                                                                                 | Note only.                                                                                                                                                                                                                                                                |
| T14 | `useOAuth` plus a static credential silently authenticates as the shared user (M3)                                                                                                                                      | `authorization`/`proxy-authorization`/`cookie` headers are rejected `400` when `useOAuth`; mutual exclusion documented.                                                                                                                                                                                                                                                                                                                                                                                                      | Endpoint: each header (any casing) → `400`, no transport call; plain connection with the same header unchanged.                                                                                                                                                           |
| T15 | Store / DCR table growth by starting and abandoning flows (M2)                                                                                                                                                          | Per-user and global entry caps with LRU eviction, client info deleted on abandon/failure when no token was written, `_CLIENT_INFO_TTL`, start rate limit.                                                                                                                                                                                                                                                                                                                                                                    | Unit: cap eviction order; abandoned flow leaves no `client_info` (store read); 6th start in a minute → `429`.                                                                                                                                                             |
| T16 | Concurrent refresh deletes a freshly rotated token (M8)                                                                                                                                                                 | Store-wide monotonic token generation counter; `delete_if_unchanged` deletes only the token the failed attempt read, including after LRU eviction and re-create.                                                                                                                                                                                                                                                                                                                                                             | Unit: failing provider + succeeding provider over one key → token survives; evict/re-create + stale failure → new token survives.                                                                                                                                         |
| T17 | SSE + `useOAuth` silently falls back to an unguarded client or bypasses the guard (M5)                                                                                                                                  | `useOAuth` on SSE is `400`; only the streamable-HTTP path can start a flow, and it carries the guard.                                                                                                                                                                                                                                                                                                                                                                                                                        | Endpoint: SSE + `useOAuth` → `400`; no `sse_client` call.                                                                                                                                                                                                                 |
| T18 | Logout regression: a required user dependency `401`s an expired-JWT logout before the cookie is cleared (M7)                                                                                                            | Identity resolved optionally; cookie cleared and `on_logout` run unconditionally; tokens and pending flows dropped only when identity resolves.                                                                                                                                                                                                                                                                                                                                                                              | Endpoint: absent/expired/invalid token → `200`, cookie cleared, `on_logout` called; raising `on_logout` → cookie still cleared, store still empty.                                                                                                                        |
| T19 | Userinfo in the MCP URL merges two tenants under one store key (M4)                                                                                                                                                     | Userinfo is rejected for `useOAuth` MCP URLs; the key never collapses two distinct URLs.                                                                                                                                                                                                                                                                                                                                                                                                                                     | Unit: `https://alice:pw@host/mcp` and `https://bob:pw@host/mcp` are refused, not merged.                                                                                                                                                                                  |
| T20 | The MCP URL path is itself an AS discovery path (`/.well-known/oauth-authorization-server`, `.../openid-configuration`), so a path-based server-origin exception would allow AS metadata (second-review H1 variant A)   | The MCP path is rejected for `useOAuth` when it contains `/.well-known/`; even if constructed directly, the AS metadata request is marked `oauth`, is not an exact PRM URL and is refused on the server origin. Only the exact PRM URLs pass.                                                                                                                                                                                                                                                                                | Unit: gate `400`; direct guard test refuses the AS metadata request, zero DCR/token requests.                                                                                                                                                                             |
| T21 | PRM `authorization_servers[0]` carries a path under `/.well-known/oauth-protected-resource/`, steering the SDK's `{P}/.well-known/openid-configuration` candidate under a prefix allowlist (second-review H1 variant B) | Exact PRM URL equality, not a prefix. No `authorization_servers[0]` can make an AS metadata candidate equal a PRM URL: an MCP path containing `/.well-known/` is rejected, and the other candidates never use the PRM prefix.                                                                                                                                                                                                                                                                                                | Unit: all three SDK discovery candidates are refused, zero DCR/token requests.                                                                                                                                                                                            |

Out of scope, and stated so nobody assumes otherwise:

- TLS termination, the MCP server's own authorization decisions, and malicious
  _developers_ (they own the config and the process).
- **Plain-path SSRF.** `POST /mcp` accepts any URL and this fork has no
  `allowed_urls`; that is a pre-existing gap. The OAuth guard closes
  **OAuth-path** steering only — it does not close, and must not be described as
  closing, SSRF through the plain (non-OAuth) transport (H1.6, R2).
- **DNS rebinding.** Even with an exact-origin allowlist, a listed hostname can
  resolve to a private address at request time. Accepted residual for v1; a
  follow-up may resolve and block private/link-local addresses at connect time.
- **Custom PRM URL on the server origin.** A `WWW-Authenticate`
  `resource_metadata` URL that is not one of the two exact PRM URLs is refused,
  and the SDK does not fall back after the guard error; such a server fails
  closed (R10). Listing the server origin as an AS origin is explicit and also
  allows AS metadata there.
- **SDK logger text.** `mcp.client.auth.oauth2` logs `OAuthFlowError` at ERROR,
  including a state mismatch (both states) or a token error body; accepted
  residual **L2b**, since our code never logs it and no client-visible body
  contains it. A redacting filter is a follow-up.
- **Logout CSRF with `SameSite=None`.** The existing `POST /logout` has no
  CSRF/`Origin` check; a cross-site POST can sign the user out and drop that
  user's MCP tokens. Accepted residual **L9**; `lax` (default) blocks it. A
  follow-up adds an `Origin`/`Sec-Fetch-Site` check.
- **AS revocation** on logout (L6 follow-up), **multi-worker stores** (L3),
  **user-identifier stability** beyond the documented requirement (L5), and the
  pre-existing wildcard CORS with credentials (L8, separate ticket).

## 7. Backward compatibility

| Surface                           | Change                                                                                                    | Compatible?                                                                           |
| --------------------------------- | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `POST /mcp` request               | New optional `useOAuth: bool = false` on the SSE and streamable-http request models                       | Yes — additive, defaulted; SSE + `useOAuth: true` is refused at runtime, not at parse |
| `POST /mcp` response              | New `202 {"status": "authorization_required", ...}` shape, only reachable with `useOAuth: true`           | Yes — unreachable for existing clients                                                |
| `POST /mcp` errors                | New `400`/`429` details for OAuth-specific inputs only                                                    | Yes — only on `useOAuth: true`                                                        |
| Existing response shape           | Unchanged (`{"success": true, "mcp": …}` / `400 {"detail": …}`) for non-OAuth connections                 | Yes                                                                                   |
| Config                            | New `features.mcp.oauth_authorization_origins` (default `[]`) and `features.mcp.user_oauth` (default off) | Yes — apps need no change                                                             |
| `GET /mcp/oauth/callback`         | New route                                                                                                 | Yes                                                                                   |
| `POST /logout`                    | Optional identity resolution + store cleanup, no response change; still `200` without a valid token       | Yes                                                                                   |
| `DELETE /mcp`                     | Extra store cleanup + pending cancel, no response change                                                  | Yes                                                                                   |
| `McpSession` (internal dataclass) | New `oauth_server_key: str \| None = None` field                                                          | Yes — internal, defaulted                                                             |
| `IMcp` (react-client)             | New optional `useOAuth`, `authorizationUrl`                                                               | Yes — TS additive; the frontend bundle ships with the backend                         |
| `localStorage` `mcp_storage_key`  | `useOAuth` kept when boolean, `authorizationUrl` dropped on rehydrate                                     | Yes — unknown keys ignored                                                            |
| Dependencies                      | None. `mcp>=2.0.0,<3.0.0` already exposes this surface (floor pinned in S2)                               | Yes                                                                                   |
| Public Python API                 | No export added to `chainlit/__init__.py`; `mcp_oauth` is internal                                        | Yes                                                                                   |

A deployment that upgrades and changes nothing behaves exactly as today. To use
the feature a developer must (1) enable authentication, (2) set `CHAINLIT_URL`,
(3) keep `CHAINLIT_COOKIE_SAMESITE` at `lax` (or `none` + `Secure`), (4) list
each authorization server's origin under
`[features.mcp].oauth_authorization_origins`, (5) set
`[features.mcp.user_oauth].enabled = true`, and (6) have users tick "Sign in with
OAuth" on a **streamable-HTTP** MCP connection.

## 8. Files and blast radius

| File                                                                                                                     | Change                                                                                                                                                                                                       | Risk                                                                |
| ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------- |
| `backend/chainlit/mcp_oauth.py`                                                                                          | **New**, ~380 lines: `normalize_origin`, store + caps + store-wide generation, pending map + timers, provider factory + `MarkedOAuthAuth`, exact `prm_urls`, allowlist guard, rate limiter                   | Isolated; pure addition                                             |
| `backend/chainlit/config.py`                                                                                             | `McpFeature.oauth_authorization_origins: list[str] = []` + validator; new `McpUserOAuthFeature` (`enabled`, `max_starts_per_user_per_minute`, `max_pending_per_user`); template comments                     | Low, but a config-shape change needs the template + docs in S6      |
| `backend/chainlit/types.py`                                                                                              | `useOAuth: bool = False` on the two HTTP request models                                                                                                                                                      | Low                                                                 |
| `backend/chainlit/server.py`                                                                                             | `/mcp` handler (~line 1939, the only high-risk edit), new `GET /mcp/oauth/callback`, `logout` optional identity, `disconnect_mcp` forget+cancel, fixed `202`/socket details, imports                         | **Highest** — shared hot path, currently untested at endpoint level |
| `backend/chainlit/session.py`                                                                                            | `McpSession.oauth_server_key`; `delete()` cancels pending flows for that session (lazy import)                                                                                                               | Low                                                                 |
| `backend/chainlit/auth/__init__.py`                                                                                      | `resolve_current_user_optional(request)` helper used by `logout` and the callback                                                                                                                            | Low — additive, no change to `get_current_user`                     |
| `backend/tests/test_mcp_oauth.py`                                                                                        | **New**: normalization matrix, full adversarial matrix, request-role marker, exact PRM set and both H1 bypass variants, store/caps/store-wide generation, pending deadlines and wakeups, provider adaptation | Pure addition                                                       |
| `backend/tests/test_mcp.py`                                                                                              | Endpoint harness for `/mcp` (fixtures a session + current user + fake transport) plus OAuth cases, pending `DELETE` key, abandoned `client_info`, and the no-OAuth regression test                           | Test-only, but larger than the production diff                      |
| `libs/react-client/src/types/mcp.ts`, `state.ts`, `useChatSession.ts`, `api/index.tsx`, `utils/mcp.ts` (new), `index.ts` | Opt-in flag, two/three socket handlers, 202 handling on reconnect, validated rehydrate, URL guard                                                                                                            | Medium — `useChatSession` reconnect path is shared                  |
| `frontend/src/components/chat/MessageComposer/Mcp/{AddForm,List}.tsx`                                                    | Checkbox (streamable HTTP only), sign-in link, cancel while pending, 400/429 detail display                                                                                                                  | Low                                                                 |
| `docs/` + `backend/chainlit/config.py` template                                                                          | Usage guide, prerequisites (`CHAINLIT_URL` https, `SameSite`, single process, SSE unsupported, custom PRM limitation), config comments                                                                       | None                                                                |

No database, no migration, no dependency change, no change to the stdio path, no
change to the plain SSE path, no change to `on_mcp_connect`/`on_mcp_disconnect`
signatures.

## 9. Proposed slices

Owner key: **D** Developer, **R** Reviewer, **S** Security Specialist, **QA** QA
Tester, **E2E** E2E Tester. Every slice ends in a PR to OneDev project 13, then
QA; S5b also needs E2E. Security review gates the merge of S3, S4a and S4b. Each
AC below is ticket-ready: it names the observable behaviour, the test level and
the verification command.

### S1 — Token store, key, caps, refresh generation (P1, D) — no dependencies

Files: `backend/chainlit/mcp_oauth.py` (store part), `backend/tests/test_mcp_oauth.py`.
Scope: `normalize_origin`, `canonical_server_key`, `McpOAuthTokenStore`,
`ScopedTokenStorage`, generations, caps/TTL, `forget_user`, `forget_server`.

- **S1-1** Separate entries per user and per server; `forget_user` and
  `forget_server` drop only their target; an empty user identifier raises.
- **S1-2** `canonical_server_key` lowercases scheme/host, drops default ports,
  maps an empty path to `/`, keeps the path verbatim (trailing slash
  significant) and the query, drops the fragment. `/mcp`, `/mcp/` and
  `/mcp?tenant=b` are three keys; URLs differing only in scheme case or an
  explicit default port share one key.
- **S1-3** `canonical_server_key` **never** drops userinfo silently: a URL with
  userinfo raises for OAuth callers, so `alice@host` and `bob@host` cannot merge.
- **S1-4** The divergence from the SDK's `resource_url_from_server_url`
  (explicit default port kept there, dropped here) is asserted in a test and
  documented in the docstring.
- **S1-5** `MAX_ENTRIES_PER_USER = 20` evicts the user's LRU entry at flow
  start; `MAX_ENTRIES_TOTAL = 500` evicts the global LRU on write; an entry that
  holds `client_info` only expires after `_CLIENT_INFO_TTL`.
- **S1-6** `forget_*` clears tokens **and** `client_info` (including the DCR
  `client_secret`); no `__repr__`, log line or exception message contains a
  token, code or client secret.
- **S1-7** `generation(key)` and `delete_if_unchanged(key, generation)` exist;
  the counter is store-wide monotonic and stamped on each **token** write
  (`set_client_info` does not change it). With two providers over one key, a
  failing refresh cannot delete the token a concurrent successful refresh wrote;
  after LRU eviction and re-create of the same key, a stale provider holding the
  old generation cannot delete the newly written token. The test covers both the
  concurrent and the eviction/re-create case.
- Verify: `cd backend && uv run pytest tests/test_mcp_oauth.py -k "store or key or cap or generation"`.

### S2 — Pending authorizations + provider factory (P1, D) — after S1

Scope: `PendingAuthorizations` (deadline, timer task, caps, cancel API),
`build_oauth_provider`.

- **S2-1** `state` is single-use. An expired entry raises
  `McpAuthorizationTimeout` **and wakes the parked waiter** (test with a 0.05 s
  TTL override, not a 300 s sleep); the timer task is cancelled on every
  terminal outcome and no entry/future/task remains.
- **S2-2** `resolve` as another user raises `PermissionError` without consuming
  the entry; the owner can still complete it afterwards. A second resolve of a
  consumed entry fails.
- **S2-3** Abandon/resolve races are safe: exactly one waiter outcome, no
  `InvalidStateError`, no double `set_result`.
- **S2-4** `MAX_PENDING_PER_USER = 3`: the 4th start raises
  `McpTooManyFlows`; the slot is released on resolve, abandon, timeout and
  cancel.
- **S2-5** `cancel_session`, `cancel_name` and `cancel_user` exist and drop only
  their target; used by S3/S4b.
- **S2-6** `callback_handler()` is bounded by the flow's remaining TTL through
  `asyncio.wait_for`; the authorization-URL wait is bounded by
  `AUTHORIZATION_URL_TIMEOUT`.
- **S2-7** The factory passes `application_type="web"`, the supplied
  `redirect_uri` verbatim and the grant types from §5.1, verified against `mcp`
  2.0.0 (floor) and 2.3.0.
- Verify: `cd backend && uv run pytest tests/test_mcp_oauth.py -k "pending or provider"`.

### S3 — Callback route, logout, refresh failure (P1, D; **S gates merge**) — after S2

Scope: `GET /mcp/oauth/callback`; `logout` optional identity; callback response
headers and fixed details; `auth.resolve_current_user_optional`.

- **S3-1** `401` unauthenticated; `400` missing code/state, unknown, expired and
  replayed state; `403` for another user's state **without consuming it** (owner
  succeeds afterwards).
- **S3-2** Success returns a plain HTML page with no token material,
  `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.
- **S3-3** No client-visible error contains `str(exc)`, a state, a URL or a
  token: the callback body, the `202` error body and
  `mcp_connection_failed {name, detail}` all carry a fixed detail from a closed
  set. A state-mismatch failure maps to a fixed detail, and a test asserts
  neither state value appears in the callback body or the socket payload. The
  SDK logger's exception text is a documented residual (L2b), not a
  client-visible body.
- **S3-4** `logout` returns `200` and clears the cookie for an absent, expired
  and invalid token; it calls `on_logout` in all three cases; a raising
  `on_logout` still leaves the cookie cleared and the user's tokens gone.
- **S3-5** `logout` forgets the user's tokens and cancels that user's pending
  flows when identity resolves, and is a no-op for the store when auth is
  disabled or no cookie is present.
- **S3-6** A refresh failure (observed by the `ChainlitOAuthProvider._handle_refresh_response`
  override) deletes only the failed token (via `delete_if_unchanged`); a token
  written by a concurrent successful refresh survives.
- **S3-7** The callback resolves under `CHAINLIT_ROOT_PATH`, and the provider's
  redirect URI shares origin+prefix with the login callback's, derived from
  `get_user_facing_url`; asserted with `CHAINLIT_ROOT_PATH` and `CHAINLIT_URL`
  set.
- Verify: `cd backend && uv run pytest tests/test_mcp.py -k "oauth_callback or logout"`.

### S4a — Origin normalization + request-role allowlist guard (P1, D; **S gates merge**) — after S1

Scope: `normalize_origin`, `OriginAllowlist`, `MarkedOAuthAuth`,
`oauth_request_guard`, `McpOAuthOriginError`, exact `prm_urls`, unit tests. This
is H1's boundary; it lands before the wiring so S4b cannot ship without it.

- **S4a-1** Exact `(scheme, host, port)` comparison: case differences, an
  explicit vs implicit default port, one trailing dot and IDN/punycode spellings
  compare equal; `https://allowed.example:8443` does not match
  `https://allowed.example`. A config entry with a path, query, fragment or
  userinfo is rejected at load, not silently reduced to its origin.
- **S4a-2** Rejects userinfo, non-`http(s)` schemes, and plain `http` outside
  loopback (`127.0.0.0/8`, `::1`, `localhost` matched by address, not by
  string); `http://10.0.0.5` is refused.
- **S4a-3** `MarkedOAuthAuth` marks the request passed into `async_auth_flow` as
  `transport` and every SDK-yielded request that is not that object as `oauth`;
  a request without a marker is refused (fail closed). The guard allows a
  `transport` request only on the server origin, and an `oauth` request only on
  a listed origin or on the server origin for an exact PRM URL — method `GET`,
  no query, URL produced by
  `build_protected_resource_metadata_discovery_urls(None, canonical_server_url)`
  plus a single trailing-slash redirect variant. No path prefix and no
  `same_path` rule remains.
- **S4a-4** A same-origin authorization server is refused unless its origin is
  listed: the test asserts AS metadata at
  `/.well-known/oauth-authorization-server`, DCR at `/register` and token
  exchange/refresh at the token endpoint on the server origin raise **and that
  the transport mock recorded zero requests** to them. It also asserts a token
  `POST` to the exact MCP endpoint path is refused (the transport marker cannot
  be inherited by an SDK-constructed request) and that an AS metadata `GET` to
  that endpoint path is refused.
- **S4a-5** Adversarial matrix: `https://allowed.example@evil.example`,
  `https://allowed.example.evil.example`, `https://evil.example/...`,
  `http://169.254.169.254/...`, `file://`, `https://allowed.example:444/...`.
- **S4a-6** Both second-review bypass variants are refused by the guard with
  zero DCR/token requests: (i) a direct guard invocation with an `oauth`-marked
  AS metadata `GET` to `/.well-known/oauth-authorization-server` (or
  `/.well-known/openid-configuration`) on the server origin; (ii) a PRM naming
  `authorization_servers[0] = https://server/.well-known/oauth-protected-resource/foo`,
  so all three SDK discovery candidates are refused. S4b-11 adds the gate-level
  `400` for variant (i).
- **S4a-7** A redirect to a non-permitted origin is refused; the rejection log
  carries only `scheme://host:port` (no path, no query). A same-origin PRM
  redirect to the trailing-slash URL still passes; any other PRM path is
  refused.
- **S4a-8** The guard's module docstring and the S6 guide state the boundary it
  is (a request-role allowlist for the OAuth flow; the server origin is not an
  AS entry) and what it is not (plain-path SSRF, DNS rebinding, custom
  `resource_metadata` URLs on the server origin). §6 T5/T20/T21 and §10 R2 in
  this note carry the same wording; no sentence claims "no residual" or that a
  path prefix closes the guard.
- Verify: `cd backend && uv run pytest tests/test_mcp_oauth.py -k "origin or guard"`.

### S4b — `/mcp` wiring, second opt-in, 202 path (P1, D; **S gates merge**) — after S2, S3, S4a

Scope: the gate chain (§5.2 step 2), second opt-in + rate limit, 202 and socket
events, per-flow deadline/cancel, `DELETE /mcp` forget, regression test for the
plain path.

- **S4b-1** `useOAuth: true` with an empty `oauth_authorization_origins` →
  `400`, and the transport factory is asserted **not called** — including when
  the PRM would name the server's own origin. No PRM or AS request is made.
- **S4b-2** `useOAuth: true` with `features.mcp.user_oauth.enabled = false` →
  `400` naming the config field; both switches on is required for a flow to
  start.
- **S4b-3** The 6th start per user per minute → `429`; the 4th concurrent
  pending flow → `429`; neither limit applies to non-OAuth connects.
- **S4b-4** `useOAuth` on `clientType: "sse"` → `400` with a fixed detail and no
  `sse_client` call; `stdio` → `400`; only streamable HTTP reaches the flow.
- **S4b-5** `useOAuth` with any casing of `authorization`,
  `proxy-authorization` or `cookie` in `headers` → `400` before any request; the
  same headers on a plain connection behave exactly as today.
- **S4b-6** `useOAuth` with `CHAINLIT_URL` unset or `http` outside loopback →
  `400` with a fixed detail.
- **S4b-7** `202` is returned at most once per connect and is followed by
  exactly one of `mcp_connected` / `mcp_connection_failed`; abandon, TTL expiry,
  websocket close and a double connect each leave no pending entry, no task and
  no attached session. The failure detail is a fixed code, never `str(exc)`.
  Abandon/timeout before a token is written leaves no tokens and no
  `client_info` for the key (the test reads the store; a concurrent successful
  token is not deleted).
- **S4b-8** `DELETE /mcp` during sign-in (no `McpSession` exists yet) uses the
  pending entry's `server_key`, cancels the flow, the callback answers `400`,
  nothing attaches and the store entry for `(user, key)` is gone; disconnecting
  after a successful connect uses `McpSession.oauth_server_key` and also removes
  the entry (the test reads the store, not the response).
- **S4b-9** Without `useOAuth`, the success body and the existing `400` body are
  byte-identical to today (regression test on the current paths).
- **S4b-10** The request guard is attached to the OAuth client only; a plain
  connection's client has no `event_hooks`.
- **S4b-11** `useOAuth` with an MCP URL whose path contains `/.well-known/`
  (case-insensitive, raw or percent-decoded) → `400` with a fixed detail and no
  transport call; this prevents variant A from being set up.
- **S4b-12** `redirect_handler` rejects a non-`http(s)` authorization URL, a URL
  with userinfo, and a plain `http` URL outside loopback → the flow fails with a
  fixed detail and the browser is not opened; the log carries only the origin.
  The frontend link displays the host (L1 residual).
- Verify: `cd backend && uv run pytest tests/test_mcp.py -k "mcp and oauth"` plus
  the existing `tests/test_mcp.py`.

### S5a — react-client (P2, D) — after S4b

`IMcp.useOAuth` / `authorizationUrl`, `utils/mcp.ts`, socket handlers, 202
handling in `useChatSession` (including the reconnect loop), validated rehydrate,
API `useOAuth` argument.

- **S5a-1** A stored entry keeps a boolean `useOAuth` and loses
  `authorizationUrl` on rehydrate; a non-boolean `useOAuth` is dropped.
- **S5a-2** The three socket handlers move status `connecting →
connected|failed` and never resurrect a deleted entry.
- **S5a-3** The `202` response does not mark the connection failed.
- **S5a-4** A `400`/`429` detail from the backend is surfaced verbatim and leaves
  the entry removable (no stuck `connecting` row).
- Verify: `pnpm --filter @chainlit/react-client test`.

### S5b — Frontend (P2, D, E2E) — after S5a

Checkbox in `AddForm` (streamable HTTP only), "Sign in" action in `List` while
pending, disconnect enabled while pending.

- **S5b-1** The checkbox is visible only for the streamable-HTTP transport and is
  never shown for SSE or stdio.
- **S5b-2** The link opens only `http(s)` URLs, with `noopener,noreferrer`, and
  only on a click; `javascript:`/`data:` are refused. The link text includes the
  authorization host so the user can see who is asking (L1 residual).
- **S5b-3** A pending server shows a sign-in action and can be removed;
  `pending → connected` and `pending → failed` both render, and a backend
  `400` detail (including "OAuth is not supported for SSE") is displayed.
- Verify: `pnpm test` (frontend), E2E against a stub authorization server.

### S6 — Docs (P2, D, R) — after S4b, before merge of the feature

`docs/` usage guide, config template comments, `upstream-triage.md` L92 updated
to "ported".

- **S6-1** A reader can go from a fresh app to a working per-user OAuth MCP
  connection without reading the code: authentication, `CHAINLIT_URL` (https
  outside loopback), `CHAINLIT_COOKIE_SAMESITE` (`lax`, or `none`+`Secure`;
  `strict` unsupported), both opt-ins, DCR requirement, token lifetime.
- **S6-2** The guide states that tokens and DCR client registrations are
  in-memory and lost on restart, that the feature is single-process only (L3),
  that `useOAuth` is unsupported on SSE and mutually exclusive with
  `authorization`/`cookie` headers, that disconnect forgets the credential, and
  that a custom `resource_metadata` URL on the server origin is not supported
  (R10) and an MCP path containing `/.well-known/` is rejected.
- **S6-3** The guide states that user identifiers must be stable and never
  reassigned (L5), that logout does not revoke at the authorization server (L6),
  that a listed authorization server may be reached by any authenticated user
  once `user_oauth.enabled` is on, and that with `SameSite=None` a cross-site
  logout is possible (L9).
- Verify: `pnpm exec prettier --check docs/research/mcp-per-user-oauth.md docs/`
  plus a manual read-through; no Python formatter applies.

Ordering: S1→S2→S3→S4a→S4b→S5a→S5b→S6. PR grouping (CEO-approved, unchanged):
**PR-1** = S1+S2 (pure additions, no wiring), **PR-2** = S3+S4a+S4b (the risky
wiring; Security review before merge), **PR-3** = S5a+S5b, **PR-4** = S6. S5a/S5b
and S6 stay P2 and may merge after PR-2 in the same release.

Estimate: ~650–850 lines of backend production code, ~250–300 of frontend, and
more test code than either (the fork has no endpoint harness for `/mcp` today, so
S4b pays for it). Revision 3 adds ~40 backend lines over revision 2 (the
`MarkedOAuthAuth` wrapper, exact PRM set, redirect-URL validation) and folds the
second review's ACs in without changing the architecture.

## 10. Risks and unknowns

| #   | Risk                                                                                                                                                            | Impact                                                                                                                           | Handling                                                                                                                                                                                                                                                                  |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | SDK 2.x adaptation: `AuthorizationCodeResult`, `application_type="web"`, `auth=` on the client not the transport                                                | A wrong port yields a flow that never completes silently                                                                         | S2 pins it with a test; the factory is the only place touching the SDK                                                                                                                                                                                                    |
| R2  | No SSRF allowlist for **plain** MCP URLs already exists in this fork                                                                                            | The OAuth path inherits the user-chosen server origin; the guard must not be credited with closing the plain path                | The guard bounds OAuth-path requests to listed origins; the plain path keeps its pre-existing gap and needs its own ticket (out of scope, flagged in §6)                                                                                                                  |
| R3  | Authorization servers that do not support dynamic client registration                                                                                           | Connection fails with a clear error; no workaround in this fork or upstream                                                      | Documented in S6; a manual `client_id` escape hatch is a follow-up if users hit it                                                                                                                                                                                        |
| R4  | Popup blocking: browsers block `window.open` outside a user gesture                                                                                             | Automatic redirect after the 202 would be blocked                                                                                | Design mandates a user click ("Sign in" action); no automatic open                                                                                                                                                                                                        |
| R5  | Reload during sign-in abandons the flow server-side until TTL                                                                                                   | User must retry; a stale `connecting` entry may show briefly                                                                     | TTL 300 s, `mcp_connection_failed` on abandonment, entry removable                                                                                                                                                                                                        |
| R6  | `canonical_server_key` and the SDK's `resource_url_from_server_url` diverge on the default port                                                                 | Two URLs that the SDK treats as distinct share one token                                                                         | Harmless (same server); documented in the store docstring and pinned by a test (S1-4)                                                                                                                                                                                     |
| R7  | Endpoint test harness (`/mcp`) does not exist                                                                                                                   | S4b's diff is mostly tests; higher chance of a thin test net                                                                     | S4b accepts a fixture-based harness as part of its scope; QA independently verifies the flow                                                                                                                                                                              |
| R8  | Security review may require binding state to a browser cookie (like `oauth_state`)                                                                              | Adds an `mcp_oauth_state` cookie and breaks multi-tab sign-in for the same server                                                | Not adopted: Security answered Q2 that caller binding is sufficient (§12)                                                                                                                                                                                                 |
| R9  | OAuth for a _user-provided_ server means users choose who authenticates them (phishing)                                                                         | Consent quality issue, not a token-theft issue (DCR + RFC 8707 bind the token to that server)                                    | The second opt-in plus the AS allowlist are the operator's levers; T9 states the residual                                                                                                                                                                                 |
| R10 | The strict PRM rule refuses a server that advertises a non-standard `resource_metadata` URL on its own origin; the SDK does not fall back after the guard error | A legitimate server fails to connect with a fixed error                                                                          | Exact PRM URLs only; the gate rejects MCP paths containing `/.well-known/`; documented in S6; a per-server PRM URL config is a follow-up if real servers need it. Transport paths are not restricted: a `transport`-marked request may use any path on the server origin. |
| R11 | Caps and rate limits surprise an operator with many users/servers                                                                                               | `429` under load; LRU eviction silently drops a credential                                                                       | Values are constants, documented in S6; eviction is logged with the user and origin, never the token                                                                                                                                                                      |
| R12 | `CHAINLIT_URL` requirement breaks a dev setup that worked before                                                                                                | `400` on `useOAuth` only; plain MCP unaffected; `http` off loopback is refused to keep the callback code/state out of clear text | Fixed detail names `CHAINLIT_URL` and its https requirement; `http://localhost` allowed for a dev loop; documented in S6                                                                                                                                                  |
| R13 | DNS rebinding of a listed authorization server                                                                                                                  | The guard matches names, not resolved addresses                                                                                  | Accepted residual in §6; follow-up may resolve and block private/link-local                                                                                                                                                                                               |
| R14 | Optional identity resolution in `logout` could mask a real auth bug                                                                                             | A broken token decoder would silently stop forgetting tokens                                                                     | `except` is narrow (decode/`HTTPException`) and logs at `debug`; S3-4/S3-5 assert both branches                                                                                                                                                                           |
| R15 | The SDK's `mcp.client.auth.oauth2` logger logs `OAuthFlowError` text (state mismatch carries both states; token error carries the body)                         | State/token material can appear in process logs, not in a client-visible response                                                | Accepted residual **L2b**: our code never logs it; a redacting logging filter is a follow-up                                                                                                                                                                              |
| R16 | Cross-site `POST /logout` when `SameSite=None` (no CSRF/`Origin` check today)                                                                                   | Forced sign-out and loss of that user's MCP tokens (re-entry, not theft)                                                         | Accepted residual **L9**; `lax` is the default and blocks the cross-site POST; follow-up adds an `Origin`/`Sec-Fetch-Site` check                                                                                                                                          |

## 11. What would change this recommendation

- **Persistence across restarts** wanted → add a `TokenStore` interface with an
  encrypted SQL implementation; S1 grows, S3/S4 do not. Recommendation becomes
  "in-memory now, interface now, persistence later".
- **Named developer-configured servers** added first (upstream parity) → the
  config-shaped variant becomes primary and `useOAuth` secondary; the store,
  guard and provider are unchanged, and the AS allowlist moves from a global set
  to a per-server field.
- **No-proxy deployment guaranteed** → Option B becomes acceptable and saves two
  socket events and the frontend 202 path, but M1's deadlines still apply.
- **Security requires per-server allowlists** → needs a server-identity config
  table this fork lacks; that is upstream's `McpServerOAuth` model and would make
  S4b depend on a named-servers ticket.
- **A real need for per-user OAuth over SSE** → add the custom
  `httpx_client_factory` (auth + guard) and a second guarded client path; the
  cost is stated in §5.2 step 2c.

## 12. Decisions closed in revisions 2–3

| #   | Question                                                             | Answer (Security + CEO)                                                                                                                                                                                                                                                                          |
| --- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Q1  | Is a global AS allowlist enough, or is a second flag needed?         | Not enough. `oauth_authorization_origins` is a hard gate and the server origin is not an AS entry; a second default-off `features.mcp.user_oauth.enabled` plus a start rate limit gate user-provided OAuth (T9).                                                                                 |
| Q2  | Is caller-bound `state` sufficient, or is a browser cookie required? | Sufficient. A cookie adds defense-in-depth but breaks multi-tab sign-in; not adopted. The `SameSite` prerequisite is documented instead (L7).                                                                                                                                                    |
| Q3  | https-only outside loopback, for the MCP URL and every AS origin?    | Mandatory, not "recommended": `https` outside loopback for the MCP URL, every configured AS origin and every request URL the SDK derives from metadata; userinfo rejected; no plain-http "internal" AS. DNS rebinding stays residual.                                                            |
| Q4  | Approve the PR grouping and P2 timing?                               | Approved. PR-1 = S1+S2, PR-2 = S3+S4a+S4b, PR-3 = S5a+S5b, PR-4 = S6; S5a/S5b/S6 stay P2 and the same release train, not a follow-up release.                                                                                                                                                    |
| M5  | SSE + `useOAuth`?                                                    | Reject with `400` in v1 (CEO). One guarded transport; revisit on demand. The alternative's cost is in §5.2 step 2c.                                                                                                                                                                              |
| Q5  | How is the server origin scoped for H1 after the second review?      | Not by URL path. `MarkedOAuthAuth` marks the MCP transport request and every SDK-constructed OAuth request; the guard allows the server origin only for `transport` and for the exact PRM URLs (GET, no query). AS metadata/DCR/token/refresh on the server origin require an explicit AS entry. |

Accepted residuals / follow-ups (documented, not blocking): **L1** (validate the
advertised authorization-endpoint origin against the AS list if cheap, else
display the host), **L2b** (SDK logger text; client-visible bodies are fixed),
**L3** (single process; S6 states it), **L5** (identifier stability documented
in S6), **L6** (no AS revocation; follow-up), **L8** (pre-existing wildcard CORS
with credentials; separate ticket), **L9** (logout CSRF under `SameSite=None`),
**DNS rebinding** (R13), **custom PRM URL on the server origin** (R10),
**access-log exposure of the callback query** (single-use code + PKCE).

## Appendix A — findings traceability

| Finding                                                                      | Design section                        | Slice ACs                     |
| ---------------------------------------------------------------------------- | ------------------------------------- | ----------------------------- |
| H1.1 empty list ⇒ `400`, no same-origin exemption                            | §5.2 step 2d, §5.4                    | S4b-1                         |
| H1.2 server origin is never an AS allowlist entry                            | §5.1 guard, §5.2 steps 4–5            | S4a-3, S4a-4, S4a-6, S4b-1    |
| H1.3 second opt-in + per-user start rate limit                               | §5.4, §5.6                            | S4b-2, S4b-3                  |
| H1.4 exact `(scheme, host, port)`, userinfo/non-http(s)/https-off-loopback   | §5.1 `normalize_origin`, §5.2 step 2h | S1-2, S1-3, S4a-1, S4a-2      |
| H1.5 required guard tests                                                    | §6 T5/T20/T21, §9 S4a/S4b             | S4a-3…S4a-6, S4b-1, S4b-11    |
| H1.6 T5/R2 must not claim plain-path SSRF is closed                          | §6 out-of-scope, §10 R2               | S4a-8                         |
| H1 variant A (MCP path is an AS discovery path)                              | §5.1 guard, §5.2 steps 2h/5           | S4a-6, S4b-11                 |
| H1 variant B (path-bearing PRM AS URL)                                       | §5.1 guard, §6 T21                    | S4a-6                         |
| M1 handshake deadline, sweep that wakes waiters, in-flight cap, cancel paths | §5.2 steps 7, 9–12, §5.6              | S2-1, S2-4, S2-5, S2-6, S4b-7 |
| M2 growth caps, DCR cleanup, TTL, rate limit                                 | §5.3, §5.6                            | S1-5, S1-6, S4b-3, S4b-7      |
| M3 header mutual exclusion                                                   | §5.2 step 2g                          | S4b-5                         |
| M4 userinfo rejected for OAuth URLs; SDK divergence stated                   | §5.1, §5.2 step 2h                    | S1-2, S1-3, S1-4, S4a-2       |
| M5 SSE + `useOAuth` = `400`                                                  | §5.2 step 2c                          | S4b-4, S5b-1                  |
| M6 `DELETE /mcp` forgets the server key, cancels the flow                    | §5.2 step 11, §8 `session.py`         | S4b-8                         |
| M7 logout optional identity, cookie always cleared, pending cancelled        | §5.5, §8 `auth/__init__.py`           | S3-4, S3-5                    |
| M8 concurrent refresh deletes only the failed token                          | §5.3 store-wide generation            | S1-7, S3-6                    |
| L2 callback headers, fixed detail, no query in application logs              | §5.2 steps 5/9/10, §5.1 guard logging | S3-2, S3-3, S4a-7, S4b-7      |
| L2b SDK logger can log exception text (accepted residual)                    | §6 T11, §10 R15, §12 residuals        | S3-3 (client-visible only)    |
| L4 DCR `client_secret` is a secret                                           | §5.3, §5.1 `forget_*`                 | S1-6                          |
| L7 `SameSite` prerequisite + `CHAINLIT_URL` required and https               | §5.4, §5.2 step 2f/2h                 | S3-7, S4b-6, S6-1             |
| L9 logout CSRF under `SameSite=None` (accepted residual)                     | §5.5, §6 out-of-scope, §10 R16        | S6-3                          |
| Config entries with a path/query/fragment/userinfo rejected at load          | §5.4                                  | S4a-1                         |
| Authorization URL scheme/userinfo/plain-http rejected                        | §5.2 step 6                           | S4b-12, S5b-2                 |
| L1 residual (advertised authorization endpoint host)                         | §5.3, §12 residuals                   | S6-3                          |
| L3 single process                                                            | §1 C10, §5.4, §6                      | S6-2                          |
| L5 identifier stability                                                      | §5.3                                  | S6-3                          |
| L6 no AS revocation                                                          | §6, §12 residuals                     | S6-3                          |
| L8 pre-existing CORS                                                         | §6 out-of-scope                       | separate ticket               |
| Process error: OneDev PR #5 link                                             | header "Review pointer"               | — (fixed in revision 2)       |
