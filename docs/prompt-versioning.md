# Prompt versioning and pinned sharing

Prompt gallery entries keep an immutable history and can be copied from a
shared prompt as a pinned, updateable copy. The version table already carries
`kind` and `description` so skills can reuse it, but skill versioning and
sharing routes are not exposed yet.

## Version contract

- Every prompt has a head row (`prompts`) and one row per version
  (`prompt_versions`). A version stores `title`, `description`, `content`,
  `contentHash`, `createdAt`, `createdBy` and optional `sourceVersionId`.
- A version row is never edited in place. Editing a prompt appends a new
  version only when the fingerprint changes. The fingerprint covers
  `kind + title + content` for prompts and adds `description` for skills.
  Toggling sharing, changing `updatedAt`, or pin metadata does not add a
  version.
- Version numbers are contiguous per prompt and guarded by a unique index on
  `("promptId", "version")`. The head update and the version insert run in one
  transaction, so a failed write leaves neither half behind.
- Old prompt rows are migrated lazily on first versioned access: version
  columns and the version/notice tables are added in place, and each existing
  prompt receives one v1 snapshot with its current content and owner.
- Limits: `MAX_PROMPT_VERSIONS = 200` stored versions per prompt and
  `MAX_PROMPT_CONTENT_BYTES = 64 KiB` per body. Exceeding either fails with a
  typed error and no partial write.

## API

All routes require the prompt gallery feature flag and a data layer that
implements the version methods; otherwise the existing `501` mapping applies.
Owners see full metadata. Readers of a shared prompt get content and version
metadata without `userId` or `createdBy`.

| Route                                                              | Purpose                                                                     |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------- |
| `GET /project/prompts/{id}/versions`                               | Version summaries, newest first. No `content`; author only for the owner.   |
| `GET /project/prompts/{id}/versions/{versionId}`                   | One version including content; sanitized for non-owners.                    |
| `GET /project/prompt/share/{id}`                                   | Public projection plus `version`, `currentVersionId`, `currentContentHash`. |
| `GET /project/prompt/share/{id}/versions`                          | Anonymous public version summaries, newest first; no author identity.       |
| `GET /project/prompt/share/{id}/versions/{versionId}`              | Anonymous public version snapshot including content; no author.             |
| `POST /project/prompt/share/{id}/add`                              | Copy a shared prompt, optionally pinning `{"versionId": "..."}`.            |
| `GET /project/prompts/updates`                                     | Pinned copies whose source is ahead and not acknowledged.                   |
| `POST /project/prompts/updates/ack`                                | `{"promptId", "version"}`; clamped to the source head, never lowers.        |
| `GET /project/prompts/{id}/diff?from=pinned\|local&to={versionId}` | Structured line rows; no HTML.                                              |
| `POST /project/prompts/{id}/sync`                                  | `{"versionId", "confirm", "expectedLocalHash"}`; append one local version.  |
| `POST /project/prompts/{id}/detach`                                | Clear the source pin; keep content and history.                             |

Public share responses never include `userId`, `createdBy`, or provenance that
is not in the six original fields plus the three version fields. The anonymous
version routes return `404` when the prompt is unshared, deleted, or the
version id does not belong to that prompt; version snapshots are sanitized the
same way as the share payload.

### Pinned copies

`add` sets `sourcePromptId`, `pinnedVersionId`, `pinnedContentHash` and
`pinnedAt` and always copies as unshared. A second copy of the same source is
rejected with `409 already_added`. When `versionId` is omitted the current
source head is pinned; an older version can be pinned explicitly.

`sync` copies the selected source version into the local head and appends one
local version. It returns `409` with a stable code:

| Code                 | Meaning                                               |
| -------------------- | ----------------------------------------------------- |
| `local_changes`      | Local edits exist; resend with `"confirm": true`.     |
| `preview_stale`      | `expectedLocalHash` no longer matches the local head. |
| `source_unavailable` | The source prompt is gone or no longer shared.        |
| `already_current`    | The local head already has the target content hash.   |
| `version_conflict`   | `versionId` does not belong to the source prompt.     |
| `not_pinned`         | The prompt has no source pin.                         |

### Updates

`GET /project/prompts/updates` compares each pinned copy with its source head.
A notice row (`prompt_update_notices`, keyed by user and prompt) stores the
highest acknowledged source version. Versions are reported when the source is
ahead of the local head, the pinned source version and the acknowledged
version. `ack` only raises that number and clamps it to the current head, so a
notice fires once per source version.

### Diff

`diff` compares the pinned snapshot (found by `pinnedContentHash` in the local
history, falling back to the source version) or the local head against a local
version id. Rows are
`{"op": "equal"|"insert"|"delete"|"replace", "oldLine", "newLine", "oldText",
"newText"}`. Responses cap at 2000 rows and 256 KiB of text and set
`truncated`; the response is JSON only, never rendered HTML.
