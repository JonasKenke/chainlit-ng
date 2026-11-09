# Sharing a thread with a named user

`[features] thread_grants` lets the owner of a thread grant read access to one
other named user. The grantee opens the existing share URL and reads the thread
without receiving the owner's identity, the owner's settings, or a write-capable
session. The flag is off by default; with it off, thread access is exactly the
owner plus the anonymous `is_shared` link.

```toml
[features]
thread_grants = true
```

The setting only takes effect when all of these are true:

- the flag is `true`,
- login is required (`[project] user_env` / `require_login()`), and
- the data layer reports `thread_grants_supported = True`.

`/project/settings` reports the result as `threadGrants`. With the flag on but
no capable layer, the grant routes return `501` and `threadGrants` is `false`,
so the UI hides the feature.

## What the owner can do

| Request                                                          | Effect                                                                                                            |
| ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `POST /project/thread/{id}/grants` `{"grantee": "<identifier>"}` | Grant read access. Idempotent. `400` for the owner, `409` at the cap, `404` for a thread the caller does not own. |
| `GET /project/thread/{id}/grants`                                | List the grants (identifier, generation, timestamp).                                                              |
| `DELETE /project/thread/{id}/grants/{grantee_user_id}`           | Revoke. Idempotent.                                                                                               |

Creation is deliberately generic: an identifier that has never signed in
returns the same `200` body as a duplicate grant but stores no row, so the
endpoint is not a one-request existence oracle. The owner sees the truth in the
list. A thread has at most 100 grants; a duplicate does not count against it.

Grant routes are owner-only and return `404` for both a missing thread and a
thread owned by somebody else. A cross-origin `Origin` header is rejected with
`403`. All grant and share responses carry `Cache-Control: private, no-store`.

## What the grantee can do

Only `GET /project/share/{id}`. The response is the same public payload as an
anonymous share link: no `userId`, no `userIdentifier`, no Dynamo keys, and no
`chat_settings`. The owner-only `GET /project/thread/{id}` and the thread list
stay owner-only. Feedback mutations stay owner-only, and the grantee never gets
a websocket session, so a read grant cannot write messages or steps.

A grant is authorized on the persisted user id **and** the user generation
(`createdAt`). A grant whose generation is missing or does not match, whose id
does not match, or whose `role` is not `read` is denied. Deleting and recreating
a user therefore invalidates old grants.

Revoking a grant takes effect on the next request; nothing is cached. A grant
does not revoke a storage URL that was already handed out (for example a
presigned element URL), and neither does the anonymous share link. Keep storage
TTLs short if that matters.

## SQLAlchemyDataLayer

`SQLAlchemyDataLayer` supports grants. The table is created lazily on the first
grant operation:

```sql
CREATE TABLE IF NOT EXISTS thread_grants (
  "threadId"               TEXT NOT NULL,
  "granteeUserId"          TEXT NOT NULL,
  "granteeUserCreatedAt"   TEXT NOT NULL,
  "granteeIdentifier"      TEXT NOT NULL,
  "grantedByUserId"        TEXT NOT NULL,
  "grantedAt"              TEXT NOT NULL,
  "role"                   TEXT NOT NULL DEFAULT 'read',
  PRIMARY KEY ("threadId", "granteeUserId")
);
CREATE INDEX IF NOT EXISTS thread_grants_grantee
  ON thread_grants ("granteeUserId", "threadId");
```

`threadId` is `TEXT` on purpose: the base `threads.id` type is operator-defined
and may be `UUID` or `TEXT`. If the application's database user cannot create
tables, create both objects by hand with the DDL above; until then grant lookup
denies, creation fails, and thread deletion keeps working.

Deleting a thread deletes its grants. Deleting all of a user's threads deletes
the grants on those threads. Calling `update_thread` without an owner (the
normal step-write path) leaves grants alone; changing the owner of a thread
deletes them.

## Custom data layers

A custom `BaseDataLayer` can opt in by implementing and then advertising:

```python
class MyDataLayer(BaseDataLayer):
    thread_grants_supported = True
    feedback_authorization_supported = True

    async def get_thread_owner(self, thread_id: str) -> Optional[ThreadOwner]: ...
    async def get_thread_grant(self, thread_id, grantee_user_id) -> Optional[ThreadGrant]: ...
    async def list_thread_grants(self, thread_id) -> List[ThreadGrant]: ...
    async def create_thread_grant(self, grant: ThreadGrant) -> None: ...
    async def delete_thread_grant(self, thread_id, grantee_user_id) -> bool: ...
    async def get_feedback_thread_id(self, feedback_id) -> FeedbackThreadLookup: ...
    async def step_belongs_to_thread(self, thread_id, step_id) -> bool: ...
```

Implementation rules:

- Override `get_thread_owner` to return the persisted user id when the layer
  knows it. The default only returns the identifier, which cannot protect
  against a recreated user.
- Return `FeedbackThreadLookup(status="error")` when the lookup itself fails;
  only `not_found` may let a feedback write insert a new row. A `found` row with
  no thread id is a legacy row and must be denied.
- `step_belongs_to_thread` must be a point check and must return `False` on any
  error.
- Honor the cleanup rules above and set the capability flags only after every
  method is implemented. The chokepoint fails closed on a layer that reports the
  capability but raises.

Grant paths must not log grantee identifiers or user ids at `INFO` or
`WARNING`. Driver errors can render query parameters, so use a quiet/redacting
query path.

## Current limits

- SQL (and any custom layer above) only. `DynamoDBDataLayer` reports no grant
  support yet, so the feature stays hidden for it.
- A grant is read-only. There is no write grant, no grantee list of shared
  threads, and no group or role-based sharing.
- The cap is a resource bound, not a security boundary; concurrent creates can
  exceed it by the number of in-flight requests.
