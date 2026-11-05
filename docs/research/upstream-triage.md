# Upstream triage: Chainlit/chainlit

Snapshot: **2026-11-04**. Upstream is archived; this fork is the maintenance
line. This note records the open PRs and issues worth porting, what has already
been ported, and the expected conflict level.

Method:

```bash
gh api -X GET search/issues -f q='repo:Chainlit/chainlit is:pr is:open' \
  -f sort=updated -f order=desc -f per_page=100
gh api -X GET search/issues -f q='repo:Chainlit/chainlit is:issue is:open' \
  -f sort=comments -f order=desc -f per_page=100
gh pr view <n> -R Chainlit/chainlit --json title,author,mergeable,mergeStateStatus,files
gh pr diff <n> -R Chainlit/chainlit
```

At snapshot time: **59 open PRs, 101 open issues**. Most PRs are
`MERGEABLE`/`BLOCKED` (no reviewers), some are `stale`, and #2798 is
`CONFLICTING`. Our `main` has diverged substantially (React 19 + Zustand,
`mcp` 2.x, LiteralAI removed, OpenAI 2.x target), so treat upstream patches as
manual ports unless they only touch data-layer or auth code.

## Already ported

| Upstream | Fix                                                                | Commit     |
| -------- | ------------------------------------------------------------------ | ---------- |
| #3070    | Verify azure-ad-hybrid OAuth state and `id_token`; per-login nonce | `c41814d6` |
| #3077    | Reentrant cache lock for nested/recursive cached calls             | `65ca0d1f` |
| #3032    | Accept `thread_id` in `step()` so `Step.__call__` works            | `f8efb2bd` |
| #3075    | Validate client-supplied websocket session IDs (path traversal)    | `55c1d069` |
| #3048    | Missing Azure AD access token returns 400, not 500                 | `55c1d069` |
| #2825    | Scope OAuth state cookies to `CHAINLIT_ROOT_PATH`                  | `55c1d069` |
| #2959    | Remove chat-context entries on disconnect (memory leak)            | `55c1d069` |
| #3076    | Preserve message tags in `MessageBase.from_dict`                   | `55c1d069` |
| #3069    | Apply `feedback=0` filters in `SQLAlchemyDataLayer`                | `55c1d069` |
| #3049    | Use `removeprefix` for DynamoDB thread/step keys                   | `55c1d069` |
| #2943    | Guard config `[meta]` without `generated_by`                       | `55c1d069` |

Compatibility work also landed in `fbba1152` (data-layer extras, removed-API
warnings, lazy-export `AttributeError`, legacy generation serialization).

Verification after the batch: 836 backend tests passed; lint, format, and mypy
clean.

## Remaining: security and privacy

### #3070 — `azure-ad-hybrid` OAuth callback (fixed)

Fixed in `c41814d6`; implementation notes and the threat model are in
[security hardening](security-hardening.md).

### #3053 — thread metadata leaks to clients

- Thread retrieval, listing, and resume events currently send all stored
  metadata; shared threads only strip a few known fields.
- Fix filters responses to `is_shared`, `viewer_read_only`, and owner-only
  `chat_profile`/`chat_settings`; storage remains unchanged.
- Size: new `thread_metadata.py` plus `server.py`/`emitter.py` changes.

## Remaining: data integrity and rendering

### #2798 — inconsistent ISO timestamps

- `ChainlitDataLayer` writes `...Z` timestamps but reads back `.isoformat()`
  without `Z`; `update_step` then fails in `strptime` (`ValueError`).
- Fix adds `_parse_iso_datetime()` and `_datetime_to_utc_iso()`; issue #2491
  has 10 comments.
- Status: upstream PR is `CONFLICTING`; port manually against our data layer.

### #2950 — resumed blobs do not render

- Uploaded PDFs, images, audio, and video use download disposition, so they
  fail to display after thread resume.
- Fix centralizes `Element.get_content_disposition()` and uses it in the
  SQLAlchemy, Chainlit, and DynamoDB layers; generic files keep attachment.
- Issue #2946 has 13 comments.

### #2915 — edited messages leave orphaned rows

- Editing a message never deletes child steps/elements from the data layer;
  historical steps accumulate and reappear in the UI.
- Delete child rows when a message is edited.

### #2799 — Base64 attachments crash thread reload

- `Element.from_dict()` stringifies bytes; data layers turn `None` into
  `"None"`; frontend resume does not resolve `chainlitKey`; storage failures
  abort resume.
- Issue #2784; spans element, data layers, and frontend.

## Remaining: state and transport

### #3062 — resumed thread replays on reconnect

- Persisted thread is replayed on every reconnect instead of only on the
  pending initial restore.
- Fix serializes resume per session and caches the completed restore; overlaps
  our new-chat race fix, so port carefully. Issue #3061.

### #3066 — unavailable chat profile leaves history unusable

- Restoring a thread whose chat profile was deleted/renamed calls resume hooks
  with invalid configuration.
- Fix validates the stored profile and falls back to the read-only history
  view. Issue #2492.

### #3067 — deleting the active thread re-selects it

- Frontend navigates to the same `/thread/:id` during deletion.
- Upstream fix is Recoil-based; our Zustand implementation needs a manual port.
  Issue #3065.

## Remaining: integrations and frontend

### #3038 — LangChain tool calls dropped

- `AIMessage.tool_calls` and `ToolMessage.tool_call_id` are not preserved in
  generation traces. Our callbacks now store plain dicts, so port manually.

### #3011 / #3056 — Markdown content fixes

- #3011 preserves unhandled remark text directives (issue #1930).
- #3056 avoids rewriting element names inside code spans/blocks.
- Both target `frontend/src/components/Markdown.tsx`, which changed heavily in
  this fork; port after the UI settles.

### #3073 / #3064 — root-path handling

- #3073 preserves the app root path in share links.
- #3064 prefixes configured asset URLs with the app base path.
- Related issue #3078: PDF preview fails when `CHAINLIT_ROOT_PATH` is set.
  Our cookie-path fix makes sub-path deployments more common, so this is
  worth doing before publishing the fork.

### #3013 — per-user OAuth for HTTP MCP connections

- Additive feature (part of issue #2197), 13 comments.
- Uses the MCP SDK `OAuthClientProvider` with a per-session token store; large
  but self-contained. Not a bug fix.

### #2954 / #3054 — CoT and side-element UX

- #2954 compact chain-of-thought display; #3054 lets side elements opt out of
  automatic expansion. Feature-level, low urgency.

## Issues with traction

| Issue                                             | Signal                    | Status / related PR               |
| ------------------------------------------------- | ------------------------- | --------------------------------- |
| #2325 headers not populated with user values      | 24 comments               | No PR found; auth investigation   |
| #1213 full `Request` object in chat               | 20 comments               | Feature/architecture              |
| #2013 Safari blocks third-party cookies (Copilot) | 15 comments               | Architectural                     |
| #2491 “Times are messed up”                       | 10 comments               | #2798                             |
| #2434 unified SQL data layer                      | 10 comments               | Design proposal                   |
| #2098 define MCP server connections               | 10 comments, 15 reactions | Verify against current MCP config |
| #1077 nested steps for tools                      | 10 comments, 7 reactions  | Feature                           |
| #1226 LaTeX handling                              | 8 comments, 10 reactions  | Frontend                          |
| #1934 persistent sidebar toggle                   | 7 comments                | Feature                           |
| #1240 replace custom OAuth                        | 7 comments, 8 reactions   | Architecture                      |
| #2340 ~4 MB initial frontend payload              | 6 comments                | Build/asset work                  |
| #1858 more input widgets                          | 6 comments, 15 reactions  | Feature                           |
| #2197 OAuth for MCP                               | 6 comments, 15 reactions  | #3013                             |
| #2094 clear all chat history                      | 2 comments, 12 reactions  | Feature                           |
| #2135 imports in custom elements                  | 2 comments, 15 reactions  | Feature                           |

### Issue → fix mapping

| Issue                             | Fix                |
| --------------------------------- | ------------------ |
| #2951 chat-context memory leak    | **Ported (#2959)** |
| #3068 negative feedback filter    | **Ported (#3069)** |
| #3061 reconnect re-resume         | #3062              |
| #3052 thread metadata leak        | #3053              |
| #2946 resumed blobs do not render | #2950              |
| #2784 Base64 reload crash         | #2799              |
| #2492 unavailable chat profile    | #3066              |
| #3078 PDF preview with root path  | No PR yet          |

## Already addressed by this fork

- #2892 LiteralAI removal.
- #3002 / #3030 `mcp` 2.x support.
- #1448 Recoil replacement (Zustand).
- #1448-adjacent `@chainlit/react-client` state-atom removal (breaking; version
  the fork as a major).

## Suggested order

1. **#3053** — thread metadata privacy.
2. **#2798** — timestamp failure in the data layer.
3. **#2950** — resumed file rendering.
4. **#3062 / #2915 / #2799** — state and persistence correctness.
5. **#3073 / #3064 / #3078** — root-path deployment fixes.
6. Frontend batch (#3011, #3056, #3067) after the React 19/Zustand UI settles.
7. Feature work (#3013, #2954, #3054).

## Caveats

- Upstream PRs are unmerged and may be stale; review each diff rather than
  assuming CI coverage.
- Cherry-picks preserve original authors; manual ports should reference the PR
  number in the commit body.
- Upstream docs (`docs.chainlit.io`) are archived and still describe removed
  APIs such as `cl.instrument_openai()`; do not copy them into fork docs.
