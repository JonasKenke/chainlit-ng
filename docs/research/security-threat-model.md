# Security baseline: threat model and automated test matrix

Status: baseline implemented; four follow-ups filed (F1 blocks completion under the
security review policy). No production code is changed by this branch.

- Date: 2026-11-07
- Base: `origin/main` at `9b8c9145` (MCP allowlist wildcard support)
- Branch: `test/security-baseline`
- Scope: backend auth (password/OAuth/JWT), cookies/sessions, uploads and element
  paths, SQLAlchemy data-layer tenancy, MCP user-server allowlist, markdown/file
  rendering boundaries.
- Prior decisions referenced, not re-derived: KRE-29 (OAuth state / Azure hybrid
  `id_token`), KRE-61 and KRE-176 (MCP allowlist and wildcards), KRE-79 (custom
  element sanitization), KRE-127 (thread erasure), KRE-110/KRE-120 (LaTeX bounds).

This document is the threat model, the coverage matrix, and the evidence record for
the security test baseline. It is not a penetration-test report: no live system was
attacked.

## 1. Assets

| Asset                                                       | Why it matters                                                     |
| ----------------------------------------------------------- | ------------------------------------------------------------------ |
| JWT signing secret and issued auth cookies                  | Full account takeover for the deployment.                          |
| OAuth authorization codes, state, nonce, `id_token`         | Account takeover / login CSRF.                                     |
| Conversations (`threads`, `steps`, `elements`) and feedback | Private user content; may contain PII and secrets pasted by users. |
| Uploaded files and data-layer blob objects                  | Malware, stored XSS, cross-tenant data.                            |
| MCP user-server credentials and OAuth tokens                | Third-party account access; SSRF pivot.                            |
| Data-layer credentials (Postgres/SQLite/DynamoDB)           | Cross-tenant read/write, availability.                             |
| Prompt gallery and scheduled tasks                          | Private prompts and automated actions.                             |
| Server availability                                         | Unbounded uploads/SQL/expensive token verification can exhaust it. |

## 2. Actors

- **Anonymous visitor**: can reach unauthenticated routes, `/project/share/*`,
  `/project/prompt/share/*`, `/health`, static assets, and the socket handshake in
  no-auth deployments.
- **Authenticated user (tenant A)**: has a valid JWT cookie or bearer token. The
  tenant boundary is the `user.identifier` / persisted `userId`.
- **Other tenant (tenant B)**: authenticated, tries to read or mutate A's threads,
  elements, files, prompts, or scheduled tasks.
- **App author**: runs the Chainlit app; defines callbacks, elements, MCP servers,
  storage and data layer. Trusted to configure the app, but not to make framework
  authorization decisions.
- **MCP server / remote HTTP origin**: external destination reached by the MCP
  client; can redirect, return hostile headers/content, or be attacker-controlled.
- **Operator/deployer**: owns secrets, allowlists, reverse proxy, cookie flags.

## 3. Trust boundaries

1. Browser → FastAPI REST (`/auth/*`, `/login`, `/project/*`, `/mcp`).
2. Browser → Socket.IO (handshake `auth` fields: `sessionId`, `threadId`, `userEnv`).
3. FastAPI → data layer (SQLAlchemy, DynamoDB, custom).
4. FastAPI → blob storage (S3/GCS/Azure).
5. FastAPI → OAuth provider (token exchange, userinfo, JWKS).
6. FastAPI → user-configured MCP servers and their redirects.
7. Server-rendered markdown → React renderer (HTML/LaTeX/custom elements).
8. Package manager → dependency graph (lockfile).

The baseline focuses on 1–7. Dependency audit covers 8.

## 4. Abuse-case matrix

Legend: **covered** = an existing test exercises the property; **added** = new test on
this branch; **follow-up** = filed as a separate issue (see §9). “Severity if
untested” is the impact if the row had no test at all, not the current finding
severity.

### 4.1 Authentication, JWT, cookies, session

| Boundary | Abuse case                                                                                  | Existing coverage                                                                                                                                                                                          | Gap / added test                                                                                                          | Level        | Severity if untested |
| -------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | ------------ | -------------------- |
| JWT      | Forge a token with another secret or tamper the signature                                   | none (only socket invalid-token)                                                                                                                                                                           | added `auth/test_jwt.py::TestDecodeJwt::test_rejects_token_signed_with_another_secret`, `test_rejects_tampered_signature` | unit         | high                 |
| JWT      | `alg: none` unsigned token                                                                  | none                                                                                                                                                                                                       | added `test_rejects_unsigned_alg_none_token`                                                                              | unit         | high                 |
| JWT      | RS256/HS256 algorithm confusion                                                             | none                                                                                                                                                                                                       | added `test_rejects_algorithm_confusion_with_rs256`                                                                       | unit         | high                 |
| JWT      | Expired token                                                                               | none                                                                                                                                                                                                       | added `test_rejects_expired_token`, `TestJwtEndpoint::test_rejects_expired_token`                                         | unit + route | high                 |
| JWT      | Missing/duplicate Authorization header, wrong scheme, malformed token                       | none                                                                                                                                                                                                       | added parameterised `TestJwtEndpoint::test_rejects_bad_credentials`                                                       | route        | medium               |
| JWT      | Missing signing secret while login is required                                              | none                                                                                                                                                                                                       | added `TestJwtSecretConfiguration`                                                                                        | unit         | high                 |
| JWT      | Valid token must set an `HttpOnly` cookie                                                   | cookie flags tested in `auth/test_cookie.py`; endpoint path untested                                                                                                                                       | added `test_accepts_valid_token_and_sets_httponly_cookie`                                                                 | route        | medium               |
| OAuth    | Missing/mismatched `state`, forged Azure hybrid `id_token`, wrong audience/issuer/nonce     | `auth/test_cookie.py::test_validate_oauth_state_cookie_*`; `test_server.py::test_oauth_callback_invalid_state_redirects`, `test_azure_hf_callback_rejects_forged_id_token`, `test_verify_azure_hf_token_*` | covered (KRE-29)                                                                                                          | unit + route | —                    |
| Cookies  | `Secure`/`SameSite`/`HttpOnly`/path, chunk reassembly and stale chunks                      | `auth/test_cookie.py::test_cookie_secure_matrix`, `test_oauth_state_and_nonce_cookie_attributes`, `test_overwrite_shorter_token_*`                                                                         | covered                                                                                                                   | unit         | —                    |
| Logout   | Invalid/expired token still clears the cookie and per-user MCP tokens; other user untouched | `test_mcp.py::TestLogout`                                                                                                                                                                                  | covered                                                                                                                   | route        | —                    |
| Socket   | Unauthenticated/invalid-token websocket connect is refused                                  | `test_socket.py::TestAuthenticateConnection`                                                                                                                                                               | covered                                                                                                                   | unit         | —                    |
| Session  | Fixation across tabs / cookie replay in a second browser                                    | none — needs a real browser                                                                                                                                                                                | E2E follow-up (§9)                                                                                                        | e2e          | medium               |

### 4.2 Uploads and file serving

| Boundary | Abuse case                                                       | Existing coverage                                                                                       | Gap / added test                                                                               | Level        | Severity if untested |
| -------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- | ------------ | -------------------- |
| Upload   | `../` or absolute `filename` escapes the session files directory | none (route mocks `persist_file`)                                                                       | added `test_security_uploads.py::TestPersistFilePaths` (real `persist_file`)                   | unit         | high                 |
| Upload   | Hostile session id becomes a path                                | none                                                                                                    | added `TestSessionIdPathComponent` (`is_valid_session_id`, `files_dir`)                        | unit         | high                 |
| Upload   | Client declares a MIME type/extension outside the allowlist      | `test_server.py::test_upload_file_mime_type_check`                                                      | covered                                                                                        | route        | medium               |
| Upload   | File larger than `max_size_mb`                                   | `test_server.py::test_upload_file_size_check`                                                           | covered; boundary added in `TestSizeLimitBoundary`                                             | route + unit | medium               |
| Upload   | Oversized body is buffered before the size check                 | none                                                                                                    | F4 follow-up (5e8a61dc)                                                                        | —            | low                  |
| Upload   | Only a foreign user's session may read its files                 | `test_server.py::test_file_access_by_different_user`                                                    | covered                                                                                        | route        | high                 |
| Download | `file_id` path traversal / guessing                              | none                                                                                                    | added `TestFileResponse::test_path_traversal_file_id_is_not_resolved`                          | route        | medium               |
| Download | Filename/CRLF reflected into response headers                    | none                                                                                                    | added `test_filename_is_not_reflected_into_response_headers`                                   | route        | medium               |
| Download | Uploaded HTML is served inline from the app origin               | none                                                                                                    | F3 follow-up (7eb11be6); response header assertion in the same test                            | —            | low                  |
| Markdown | `language` query selects a file outside the app root             | `test_server.py::test_project_settings_path_traversal`, `test_project_translations_file_path_traversal` | covered at the route; added `test_markdown.py::TestGetMarkdownStrTraversal` for direct callers | unit         | high                 |

### 4.3 Threads, elements, tenancy

| Boundary   | Abuse case                                                         | Existing coverage                                                             | Gap / added test                                                                                                                                           | Level        | Severity if untested |
| ---------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | -------------------- |
| Threads    | Read/rename/share/delete another user's thread                     | none at the HTTP ACL layer                                                    | added `test_security_tenancy.py::TestThreadAcl` (5 routes, mutation not reached)                                                                           | route        | high                 |
| Threads    | Read another user's element via thread/element route               | none                                                                          | covered by the same parameterised ACL test                                                                                                                 | route        | high                 |
| Threads    | Client supplies `filter.userId` to list another tenant's threads   | none                                                                          | added `TestThreadListFilter::test_client_supplied_user_id_is_overwritten`                                                                                  | route        | high                 |
| Threads    | Shared-thread endpoint leaks `env`/`chat_settings`/custom metadata | `test_thread_metadata.py` (filter), share flag tested in `test_server.py`     | added `TestSharedThreadExposure` (unshared=404, metadata filtered, callback denial)                                                                        | route        | high                 |
| Elements   | Custom element update/delete by a different tenant                 | sanitization only: `test_element.py::test_custom_element_sanitize_*`          | **F2**; `test_cross_tenant_custom_element_delete_is_rejected` (xfail strict)                                                                               | route        | medium               |
| Elements   | Element object key escapes the owning user prefix                  | none                                                                          | added `test_sql_alchemy.py::test_element_storage_key_stays_under_the_thread_owner`                                                                         | unit         | medium               |
| SQL        | `delete_all_threads` deletes another user's rows                   | `test_sql_alchemy.py::test_delete_all_threads_only_deletes_target_user`       | covered (KRE-127)                                                                                                                                          | unit         | critical             |
| SQL        | `list_threads` / favorites / prompts return another user's rows    | none                                                                          | added `test_list_threads_only_returns_target_user_threads`, `test_favorite_steps_only_returns_target_user_steps`, `test_prompts_are_scoped_to_their_owner` | unit         | high                 |
| SQL        | `get_element` ignores the thread id                                | none                                                                          | added `test_get_element_is_scoped_to_its_thread`                                                                                                           | unit         | high                 |
| SQL        | Client-supplied thread id injects SQL into step/element reads      | none                                                                          | **F1**; `test_thread_id_cannot_inject_sql` (xfail strict)                                                                                                  | unit         | high                 |
| Prompts    | Update/delete another user's prompt                                | `test_prompt_gallery.py::test_update_prompt_not_owner` (delete scoped in SQL) | covered                                                                                                                                                    | route + unit | high                 |
| Scheduling | Update/run/delete another user's scheduled task                    | `test_prompt_scheduling.py::test_owner_and_validation_rejection`              | covered for update; SQL delete is user-scoped                                                                                                              | route + unit | high                 |
| Share      | Unknown thread returns 500 rather than 404                         | none                                                                          | observation only: `TestThreadAcl::test_missing_thread_does_not_reach_the_data_layer`; no body leaked                                                       | route        | low                  |

### 4.4 MCP and SSRF

| Boundary  | Abuse case                                                                                           | Existing coverage                                                                | Gap / added test                                                        | Level | Severity if untested |
| --------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | ----- | -------------------- |
| Allowlist | URL not in operator allowlist, wrong scheme/port/path subtree                                        | `test_mcp.py::test_validate_mcp_url`                                             | covered (KRE-61)                                                        | unit  | high                 |
| Allowlist | Host wildcard matches apex/lookalike/IP                                                              | same parametrised test                                                           | covered (KRE-176)                                                       | unit  | high                 |
| Allowlist | Traversal/encoded separators/Unicode dots/`;params`/`%u` in path                                     | same test                                                                        | covered                                                                 | unit  | high                 |
| Allowlist | Parser differentials: userinfo, backslash authority, percent-encoded host, trailing dot, port suffix | none                                                                             | added 5 rows to `test_validate_mcp_url`                                 | unit  | high                 |
| Transport | Redirect leaves the allowlist                                                                        | `test_mcp.py::TestOAuthTransportAllowlist`                                       | covered                                                                 | unit  | high                 |
| Transport | User `Authorization`/`Cookie`/`Host`/forwarded headers override the app's                            | `test_mcp.py::TestUserProvidedMcpServers`, `TestValidateMcpHeaders`              | covered                                                                 | unit  | high                 |
| Transport | Non-allowlisted URL refused before any outbound call                                                 | `test_mcp.py::test_non_allowlisted_user_url_is_refused_before_any_outbound_call` | covered                                                                 | unit  | high                 |
| DNS       | Allowlisted hostname resolves to a private address (rebinding)                                       | none                                                                             | residual risk: allowlist is hostname-based by design; operator owns DNS | —     | medium               |

### 4.5 Rendering and data exposure

| Boundary        | Abuse case                                                | Existing coverage                                                                            | Gap / added test                                                                        | Level    | Severity if untested |
| --------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | -------- | -------------------- |
| Markdown        | Raw HTML/script from a message executes in the app origin | renderer is browser-only; `unsafe_allow_html` defaults to `false` and is documented as risky | E2E follow-up (§9), not a backend test                                                  | e2e      | medium               |
| LaTeX           | Unbounded `$`/`$$` input causes renderer DoS              | KRE-110/KRE-120 frontend bounds                                                              | covered outside this backend matrix                                                     | e2e/unit | —                    |
| Custom elements | Props shape must be sanitized before update               | `test_element.py::test_custom_element_sanitize_update_preserves_collapsed_hint`              | covered (KRE-79); ownership is F2                                                       | unit     | medium               |
| Threads         | Internal blob key / signed URL returned to shared viewers | `test_thread_metadata.py` filters metadata, not element fields                               | residual risk: element URLs are needed for rendering; storage objects should be private | —        | low                  |
| Errors          | Exceptions returned to clients                            | route handlers map to generic 401/404/500; no stack traces in responses                      | observation only                                                                        | —        | low                  |

## 5. Findings

### F1 (high) — second-order SQL injection via client-supplied thread id

**Where:** `backend/chainlit/data/sql_alchemy.py:762` builds
`thread_ids = "('" + "','".join(...) + "')"` from stored thread ids and interpolates it
into the steps and elements queries. `backend/chainlit/socket.py::connect` accepts
`auth.threadId` without UUID validation, stores it as `session.thread_id`, and
`SQLAlchemyDataLayer.update_thread()` persists it. The next `get_thread` /
`list_threads` read interpolates it.

**Exploit path:** connect the socket with `threadId` = a crafted string; send one
message so the thread row is created; call `POST /project/threads` (or resume) to run
`get_all_user_threads`. A UNION payload reads another tenant's `steps.output` into the
attacker's thread view.

**Impact:** cross-tenant read of conversation content; arbitrary SQL under the data
layer role where the statement permits it. Confirmed against the SQLite schema used by
the tests. PostgreSQL's UUID primary key rejects the insert, which limits exposure
there but does not fix the interpolation.

**Repro:** `backend/tests/data/test_sql_alchemy.py::test_thread_id_cannot_inject_sql`
(`xfail(strict=True)`). It fails today with `VICTIM-SECRET` in the attacker's result.

**Fix:** bind thread ids as parameters (expanding bindparam) in the steps/elements
queries; validate `threadId` at the socket boundary; remove the xfail marker.
Filed as `e7a29b68-9e66-43cb-be3b-7b597d9dac51` (high, Developer).

### F2 (medium) — missing object-level authorization on `/project/element`

**Where:** `backend/chainlit/server.py::update_thread_element` /
`delete_thread_element`. The caller's session must match the authenticated user, but
the target element's owner is never checked. `CustomElement.update()` upserts by id;
`CustomElement.remove()` → `data_layer.delete_element(id, thread_id)` deletes by id
only.

**Exploit path:** tenant B learns tenant A's custom element id (shared threads include
element ids) and sends PUT/DELETE `/project/element` with B's own session. B can
overwrite the element content or delete the row/blob.

**Impact:** cross-tenant integrity loss. UUID ids and the need to learn the id reduce
likelihood; shared threads supply the id.

**Repro:** `backend/tests/test_security_tenancy.py::test_cross_tenant_custom_element_delete_is_rejected`
(`xfail(strict=True)`): the route answers 200 and calls
`data_layer.delete_element("alice-element-id", "alice-thread")`.

**Fix:** resolve the element's thread and require the caller to be its author before
update/delete; scope the data-layer mutation by `(thread_id, element_id)`; remove the
xfail marker. Filed as `1fe21a5c-f432-433e-bb53-22b00657b162` (medium, Developer).

### F3 (low) — uploaded non-media files are served inline without `nosniff`

`GET /project/file/{id}` returns the client-declared MIME type with no
`Content-Disposition` and no `X-Content-Type-Options: nosniff`. An HTML upload opened
directly renders from the Chainlit origin (no-auth deployments; direct navigation).
The UI's `download` link reduces the normal path. Filed as
`7eb11be6-44e2-4667-b955-e8e749143b9c` (low). Verified non-issue: filenames are not
reflected into response headers (`TestFileResponse`).

### F4 (low) — upload body read before size validation

`upload_file()` reads the entire `UploadFile` before calling `validate_file_upload()`,
so `max_size_mb` does not bound memory. Filed as
`5e8a61dc-dccf-49c0-9703-4d987a4073c2` (low). No flaky memory-pressure test added;
the control-flow ordering is the evidence.

## 6. Verified mitigations (keep these)

- OAuth state cookie is checked with `hmac.compare_digest`; Azure hybrid verifies the
  `id_token` signature, audience, issuer and per-login nonce (KRE-29). Covered by the
  existing `test_cookie.py` / `test_server.py` matrix.
- JWT decoding pins `algorithms=["HS256"]` and verifies signatures and expiry.
- Cookie flags/path/chunk cleanup are tested; logout clears per-user MCP tokens and
  leaves other users untouched.
- `is_valid_session_id` confines the session files directory; `persist_file` uses a
  UUID for the stored filename and the client name only as metadata.
- `is_thread_author` gates thread/element read, rename, share and delete; the new
  matrix proves the mutators are not reached for another user.
- Shared-thread responses pass through `public_thread()`, which allowlists
  `is_shared`/`viewer_read_only` (and settings only for the owner).
- SQLAlchemy thread deletion is scoped by `userId`; prompts and scheduled tasks are
  scoped by `userId` in SQL and by an explicit owner check in the routes.
- MCP user URLs are validated against the operator allowlist, userinfo/header
  overrides are stripped, redirects are not followed, and the OAuth transport hook
  re-checks every redirect target.
- Custom element updates are rebuilt from an allowlisted field set and non-custom
  types are rejected.

## 7. Static checks

### Dependency audit

Command: `uv run --with pip-audit pip-audit --path .venv/lib/python3.13/site-packages`
(installed test environment, Python 3.13, `uv.lock` at `9b8c9145`).

Result: **1 finding**

| Package    | Version | Advisory       | Fix   | Dependency path                                                  | Triage                                                                                                                                                                  |
| ---------- | ------- | -------------- | ----- | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `oauthlib` | 4.0.0   | CVE-2026-49272 | 4.0.1 | `botbuilder-core` (tests extra) → `msrest` → `requests-oauthlib` | low: not in the base runtime export; test/Teams-integration dependency only. Do not auto-bump here; refresh the lock or add a constraint in the dependency-update work. |

`uv export --no-dev` contains no `oauthlib` or `botbuilder` entry, so a default
`pip install chainlit` is not affected. No action taken in this ticket (non-goal: no
dependency changes).

### Secrets scan

Command: a tracked-file scan over `git ls-files` (759 files) for private-key blocks,
AWS access keys, GitHub/npm/Slack/Google/OpenAI/Stripe token formats, excluding
lockfiles and minified bundles. Results: **0 matches**. No tracked `.env`, `.pem`,
`.key`, `id_rsa` or credentials files. The scan prints pattern names and paths only;
no values were printed or stored. Test fixtures use obvious fake constants (e.g.
`test-secret-...`) and are not credentials.

## 8. Verification

- `cd backend && uv run pytest -q`: **1393 passed, 2 xfailed**.
- After the pre-commit formatter: targeted security files **106 passed, 2 xfailed**.
- `uv run scripts/lint.py`: all checks passed.
- `uv run scripts/format.py --check`: 230 files already formatted.
- `uv run scripts/type_check.py`: no issues in 126 source files.
- `git diff -- backend/chainlit` after all mutation checks: empty (no production
  changes).

The workspace is shared with another concurrent run. Later full-suite reruns under
load average > 70 on a 2-vCPU host hit the pre-existing 15-second subprocess timeout
in `tests/test_cache.py::test_cache_nested_and_recursive_calls`; that test passes in
isolation when import time is normal and is unrelated to this branch. The green run
above is the verification baseline.

## 8.1 Mutation evidence

Each mutation was applied to a production file, the targeted tests were run, and the
file was restored from its original copy. `git diff -- backend/chainlit` is empty after
the checks.

| Mutation                                           | Test                                         | Result                                                    |
| -------------------------------------------------- | -------------------------------------------- | --------------------------------------------------------- |
| `decode_jwt`: `verify_signature: True` → `False`   | `auth/test_jwt.py`                           | 6 failed, 10 passed — signature/algorithm tests detect it |
| `is_thread_author`: always allow                   | `test_security_tenancy.py`                   | 5 failed (all ACL routes), 1 xfailed                      |
| `validate_mcp_url`: skip `_has_ambiguous_path`     | `test_mcp.py -k validate_mcp_url`            | 24 failed, 44 passed                                      |
| `persist_file`: store under `name` instead of UUID | `test_security_uploads.py`                   | 5 failed (traversal cases)                                |
| `get_all_user_threads`: drop `userId` predicate    | `test_sql_alchemy.py -k only_returns_target` | 1 failed (tenant list test)                               |

The two `xfail(strict=True)` tests are the current repros for F1/F2. If either fix
lands, the strict xfail turns into an XPASS failure, forcing the marker to be removed.

## 9. Follow-ups and E2E list

Filed on the board as children of this issue (Developer owner):

| ID                                     | Severity | Item                                                   |
| -------------------------------------- | -------- | ------------------------------------------------------ |
| `e7a29b68-9e66-43cb-be3b-7b597d9dac51` | high     | F1: bind thread ids in SQLAlchemy step/element queries |
| `1fe21a5c-f432-433e-bb53-22b00657b162` | medium   | F2: enforce element ownership on `/project/element`    |
| `7eb11be6-44e2-4667-b955-e8e749143b9c` | low      | F3: attachment/nosniff headers for uploaded files      |
| `5e8a61dc-dccf-49c0-9703-4d987a4073c2` | low      | F4: validate upload size before buffering the body     |

Browser-only cases for the E2E Tester (not backend tests):

1. `unsafe_allow_html = true` with payloads in assistant/user/tasklist text: confirm
   script/event-handler/iframe/`javascript:` URLs do not execute, and that the
   documented opt-in is the only way raw HTML is parsed.
2. LaTeX bounds (KRE-110/KRE-120): large/deeply nested `$` and `$$` inputs do not hang
   the renderer.
3. Session fixation across tabs: log in in tab A, set a stale session id in tab B,
   verify the socket refuses or re-authenticates and no cross-user state is shown.
4. Shared thread opened in a second account: only owner-approved metadata is visible;
   element ids cannot be used with `/project/element` after F2 lands.
5. Upload an HTML file in a no-auth deployment and open its file URL directly after
   F3 lands: it must download (or be inert), not execute.

## 10. Residual risk

- DNS rebinding for an allowlisted MCP hostname is not addressed; the allowlist is an
  operator policy over names, not IPs. Deployments that need network-level SSRF
  protection should add egress filtering.
- The MCP allowlist trusts the operator's entries; a wildcard over a shared domain
  admits every tenant under it (documented in KRE-176).
- Default spontaneous uploads allow `*/*` and 500 MB; F4 covers the memory bound, F3
  covers inline execution. Deployments should override both.
- No rate limiting is built into Chainlit; login, OAuth start, MCP OAuth and upload
  endpoints rely on a reverse proxy for request-rate limits.
- The SQLite data layer is convenient but its UUID columns accept text; F1's fix must
  not rely on database type enforcement.
- TestClient/mocks cannot prove browser behavior; the E2E list is the remaining gap.
- The dependency audit covers the installed environment, not every optional
  integration; production installs should run `pip-audit` on their own lock.

## Verdict

`pass with follow-ups` for the baseline deliverables (matrix, tests, static checks, no
production changes). Under the security review policy the high-severity F1 blocks
closing the parent security work until fixed or explicitly accepted by the CEO; F2 is
medium, F3/F4 are low. The baseline itself is safe to merge because it only adds tests
and documentation.
