# Input validation: negative-path inventory and findings (KRE-187)

Status: **complete** — tests added, findings filed for follow-up. No production
code was changed by this pass.

- Base commit: `9b8c9145` (`origin/main` at 2026-11-07)
- Branch: `test/input-negative-paths`
- Tests: `backend/tests/test_input_negative_paths.py` (59 cases: 36 pass,
  23 strict `xfail` pinning a confirmed defect)
- Cross-reference: KRE-186 security baseline. This pass covers correctness of
  validation and error handling; it does not repeat the security matrix. One
  authorisation defect (finding F4) is included because it is an input-trust
  bug on an endpoint in scope.

## Method

1. Enumerated every user-controlled field on the in-scope HTTP and Socket.IO
   surfaces and read the literal limit (or its absence) in code.
2. Exercised each violation with `fastapi.testclient.TestClient` and a mock
   `BaseDataLayer`; the two pagination cases use the real
   `SQLAlchemyDataLayer.list_threads` with `get_all_user_threads` stubbed.
3. A deviation is committed as a strict `xfail` with a finding ID. When the
   defect is fixed, the test XPASSes and fails, forcing the marker to be
   removed.

Run:

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py -q
uv run pytest tests/test_input_negative_paths.py --runxfail -q   # shows observed defects
```

## Inventory and boundary table

Legend: **covered** = an existing or new test asserts the behaviour;
**gap** = an input path with no test before this pass; **finding** = current
behaviour is wrong and is recorded below. `test_negative` below means
`backend/tests/test_input_negative_paths.py::<name>`.

### 1. Thread list — `POST /project/threads`

Handler `backend/chainlit/server.py:1338`. Models `Pagination`, `ThreadFilter`
(`backend/chainlit/types.py:55-64`).

| Field                                 | Accepted / literal limit                                                   | Violation behaviour                                                                 | Coverage                                                                                                                              |
| ------------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `pagination.first`                    | Python `int`, no lower/upper bound                                         | `-1` returns 200 with `filtered[start:-1]` (all but last); a huge value returns 200 | **finding F6**; `test_negative::test_threads_negative_page_size_returns_422` (xfail), `test_threads_malformed_pagination_returns_422` |
| `pagination.first` missing/wrong type | Pydantic `int`                                                             | 422                                                                                 | **covered** `test_threads_malformed_pagination_returns_422`                                                                           |
| `pagination.cursor`                   | `Optional[str]`, no length bound                                           | Unknown cursor starts at 0 (silent)                                                 | no defect found; not tested (low value)                                                                                               |
| `filter.feedback`                     | `Literal[0, 1]`                                                            | 422 for `2`                                                                         | **covered** `test_threads_malformed_pagination_returns_422`                                                                           |
| `filter.userId`                       | Client value is overwritten with `current_user.id` (`server.py:1352-1357`) | Client cannot read another user's list                                              | **covered** `test_threads_filter_user_id_is_forced_to_current_user`                                                                   |
| `filter.search`                       | `Optional[str]`, no length bound; Python `in` match in SQLAlchemy          | Very long string is scanned in memory                                               | not a correctness defect for SQLAlchemy; DynamoDB implementation differs                                                              |
| No data layer                         | —                                                                          | 400                                                                                 | existing route tests in `test_server.py`                                                                                              |
| No auth                               | —                                                                          | 401                                                                                 | **covered** `test_thread_endpoints_require_authentication` (for thread routes)                                                        |

### 2. Thread read / rename / share / delete

Handlers `server.py:1367` (read), `1949` (rename), `1974` (share), `2026`
(delete), `1841` (element read). Models `UpdateThreadRequest:221`,
`ShareThreadRequest:226`, `DeleteThreadRequest:231`.

| Field / violation              | Accepted / literal limit                            | Violation behaviour                                                                                                                                                                                                                                    | Coverage                                                                                               |
| ------------------------------ | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------ |
| `threadId` of a missing thread | `str`, no format check                              | `is_thread_author()` (`data/acl.py:6`) expects `get_thread_author()` to return a falsy value, but every built-in layer raises `ValueError` (`sql_alchemy.py:205`, `chainlit_data_layer.py:457`, `dynamodb.py:406`). Result: **500** on all five routes | **finding F1**; `test_missing_thread_returns_404` (xfail, 5 cases)                                     |
| Requester is not the author    | —                                                   | 401                                                                                                                                                                                                                                                    | **covered** `test_get_thread_rejects_non_author`, `test_get_element_rejects_non_author`                |
| No auth / no data layer        | —                                                   | 401 / 400                                                                                                                                                                                                                                              | **covered** `test_thread_endpoints_require_authentication`, `test_thread_endpoints_require_data_layer` |
| `name` missing                 | Required `str`                                      | 422                                                                                                                                                                                                                                                    | **covered** `test_rename_thread_missing_fields_returns_422`                                            |
| `name` empty / whitespace      | `str`, no `min_length`, no strip                    | 200; UI requires a value                                                                                                                                                                                                                               | **finding F10**; `test_rename_thread_rejects_blank_name` (xfail)                                       |
| `isShared` missing / not bool  | Pydantic `bool` (coerces `"true"`, `1`)             | 422 if absent; coercion accepted                                                                                                                                                                                                                       | Pydantic behaviour, no defect                                                                          |
| Share a missing thread         | —                                                   | 500                                                                                                                                                                                                                                                    | **finding F1**                                                                                         |
| Delete a missing thread        | —                                                   | 500                                                                                                                                                                                                                                                    | **finding F1**                                                                                         |
| `DELETE /project/threads`      | No body; auth + layer checks; unsupported layer 501 | tested in `test_server.py::test_delete_all_threads_*`                                                                                                                                                                                                  | **covered** existing                                                                                   |

### 3. Shared-thread read — `GET /project/share/{thread_id}`

Handler `server.py:1396`. No auth by design; sharing is a metadata flag.

| Violation                           | Behaviour                                                | Coverage                                                                                                   |
| ----------------------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Thread missing                      | 404                                                      | **covered** `test_shared_thread_missing_returns_404`                                                       |
| Thread exists but `is_shared` false | 404 (no existence leak)                                  | **covered** `test_shared_thread_unshared_returns_404`                                                      |
| Thread shared                       | 200, metadata filtered to `is_shared`/`viewer_read_only` | **covered** `test_shared_thread_is_public`; `test_thread_metadata.py::test_public_thread_filters_metadata` |
| `on_shared_thread_view` raises      | Treated as "not allowed" -> 404                          | code path `server.py:1429-1434`; no test                                                                   |
| No data layer                       | 400                                                      | existing behaviour                                                                                         |

### 4. File upload — `POST /project/file`

Handler `server.py:3203`; validation `server.py:3257-3336`. Config model
`SpontaneousFileUploadFeature` (`config.py:338-342`; defaults `max_files=20`,
`max_size_mb=500` at `config.py:145-146`). Per-message `AskFileSpec` from
`AskFileMessage` (`message.py:458-490`).

| Field / violation                       | Accepted / literal limit                                                                                                | Violation behaviour                                                                                            | Coverage                                                                                                            |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `session_id` unknown                    | Must resolve to a live `WebsocketSession`                                                                               | 404                                                                                                            | **covered** `test_upload_rejects_unknown_session`                                                                   |
| Session owned by another user           | Compared to `current_user.identifier`                                                                                   | 401                                                                                                            | **covered** `test_upload_rejects_other_users_session`                                                               |
| `file` part missing                     | Required `UploadFile`                                                                                                   | 422                                                                                                            | **covered** existing `test_upload_file_missing_file`                                                                |
| `file.content_type` missing             | `assert file.content_type` (`server.py:3234`)                                                                           | **500**                                                                                                        | **finding F3**; `test_upload_without_content_type_returns_400` (xfail)                                              |
| `file.filename` empty                   | `assert file.filename` (`server.py:3233`)                                                                               | 500 in code; standard multipart encodes an empty filename as a form field, so the request fails with 422 first | code-level; same fix as F3                                                                                          |
| MIME not in `accept`                    | `fnmatch` on client-supplied `content_type`; dict form also checks extension                                            | 400                                                                                                            | **covered** existing `test_upload_file_mime_type_check`                                                             |
| `file.size > max_size_mb * 1024 * 1024` | `file.size` from Starlette, integer MB                                                                                  | 400                                                                                                            | **covered** existing `test_upload_file_size_check`                                                                  |
| `ask_parent_id` unknown                 | Must exist in `session.files_spec`                                                                                      | 404                                                                                                            | **covered** `test_upload_unknown_ask_parent_returns_404`; existing `test_ask_file_with_spontaneous_upload_disabled` |
| `AskFileSpec.max_files`                 | Set by `AskFileMessage(max_files=...)`; **never read by the server** (`validate_file_upload` only checks MIME and size) | Extra uploads accepted silently; only the frontend enforces the count                                          | **finding F11**; `test_upload_enforces_ask_max_files` (xfail)                                                       |
| Zero-byte file                          | No `min_size` anywhere                                                                                                  | 200, persisted with empty content                                                                              | documented behaviour: `test_upload_accepts_zero_byte_file`                                                          |
| Filename path traversal                 | Disk name is a random UUID + MIME extension (`session.py:224-250`); original name only in response                      | Safe by construction                                                                                           | not tested (no filesystem path uses the name)                                                                       |
| Huge file                               | Entire body is read into memory (`await file.read()`) before the size check                                             | Memory pressure before rejection                                                                               | not a correctness defect; performance/DoS out of scope                                                              |

### 5. File read — `GET /project/file/{file_id}`

Handler `server.py:3338`.

| Violation            | Behaviour                                                | Coverage                                      |
| -------------------- | -------------------------------------------------------- | --------------------------------------------- |
| Unknown session      | 401                                                      | existing `test_get_file_non_existing_session` |
| Other user's session | 401                                                      | existing `test_file_access_by_different_user` |
| Unknown file id      | 404                                                      | existing `test_get_file_not_existent_file`    |
| Path-like `file_id`  | Dict lookup in `session.files`, no filesystem path built | safe by construction                          |

### 6. Custom elements — `PUT` / `DELETE /project/element`, `GET .../element/...`

Handlers `server.py:1863`, `1899`, `1841`; sanitizer `server.py:1934`.
`ElementRequest.element` is an untyped `Dict` (`types.py:290`).

| Field / violation                        | Accepted / literal limit                                                      | Violation behaviour                                        | Coverage                                                                                                                             |
| ---------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `sessionId` unknown                      | Must resolve to a live session                                                | **500** (`WebsocketSession.require` raises `ValueError`)   | **finding F2**; `test_element_update_unknown_session_returns_4xx` (xfail)                                                            |
| Required `id`, `name`, `display` missing | Direct indexing in `_sanitize_custom_element`                                 | **500** (`KeyError`)                                       | **finding F2**; `test_element_malformed_payload_returns_422` (xfail, 4 cases incl. non-dict `props`)                                 |
| `type != "custom"`                       | Only custom elements are mutable                                              | 200 `{"success": false}`                                   | documented: `test_element_update_rejects_non_custom_type`                                                                            |
| `threadId`                               | Client-controlled; not compared with `session.thread_id` or the session owner | Another user's thread/element id can be written or deleted | **finding F4**; `test_element_update_does_not_trust_client_thread_id`, `test_element_delete_does_not_trust_client_thread_id` (xfail) |
| `forId`                                  | Client-controlled message id                                                  | Element can be attached to any message id                  | part of F4                                                                                                                           |
| `element_id` missing                     | `get_element()` returns `None`                                                | 200 with body `null`                                       | **finding F5**; `test_get_missing_element_returns_404` (xfail)                                                                       |
| Non-author reading an element            | `is_thread_author`                                                            | 401                                                        | **covered** `test_get_element_rejects_non_author`                                                                                    |

### 7. Action callbacks — `POST /project/action`

Handler `server.py:2082`; `CallActionRequest.action: Dict` (`types.py:244`);
`Action` requires `name: str`, `payload: Dict` (`action.py:12-16`).

| Violation                                         | Behaviour                            | Coverage                                                                                      |
| ------------------------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------- |
| `sessionId` unknown                               | **500** (`WebsocketSession.require`) | **finding F2**; `test_action_unknown_session_returns_404` (xfail)                             |
| `action` missing `name`/`payload`, or wrong types | **500** (`Action(**dict)` raises)    | **finding F2**; `test_action_malformed_action_returns_422` (xfail, 4 cases)                   |
| Callback name not registered                      | 404                                  | **covered** `test_action_unknown_callback_returns_404`                                        |
| Session owned by another user                     | 401                                  | existing `test_unavailable_chat_profile.py::test_http_action_respects_readonly_and_ownership` |
| Read-only profile                                 | 409                                  | existing same test                                                                            |
| No `sessionId` / non-dict `action`                | Pydantic 422                         | route model                                                                                   |

### 8. Socket.IO message send — `client_message`

Handler `socket.py:549` -> `process_message` (`socket.py:435`) -> emitter
(`emitter.py:322`). Payload is the `MessagePayload` TypedDict
(`types.py:164`); at runtime TypedDict is not validated.

| Payload / violation                       | Accepted / literal limit                                           | Behaviour                                                                                      | Coverage                                                                    |
| ----------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `message.id`                              | Must parse as a v4 UUID (`emitter.py:326`, `assert`)               | `AssertionError` is caught; an `isError` assistant message is emitted; app callback not called | **covered** `test_socket_malformed_message_emits_error_without_calling_app` |
| `message.id` / `createdAt` missing        | Direct indexing in `Message.from_dict` (`message.py:60`)           | caught, error message emitted                                                                  | **covered** same                                                            |
| `message.output` empty or whitespace-only | No check                                                           | accepted; `on_message` runs with `""` / whitespace                                             | **finding F7** (behaviour documented by passing tests)                      |
| `message.output` size                     | No application limit; Engine.IO buffer is the only transport cap   | 1 MB accepted by the application layer                                                         | **finding F7**; `test_socket_oversized_message_is_accepted`                 |
| `message.output` non-string               | Pydantic/Step serialises it (dict becomes a JSON string)           | accepted                                                                                       | documented; no defect found                                                 |
| `fileReferences` unknown ids              | Unknown ids are dropped (`emitter.py:330-335`)                     | accepted; no element attached                                                                  | intended; not tested                                                        |
| `edit_message` with unknown id            | No matching message                                                | silently no-ops                                                                                | low-value, not tested                                                       |
| Concurrent `client_message`               | `session.current_task` is replaced; previous task is not cancelled | both messages are processed                                                                    | browser-only double-send; E2E list                                          |

### 9. Chat-profile selection — Socket.IO `connect`

Handler `socket.py:197`; `chatProfile` is URL-decoded and stored without
validation (`socket.py:241-248`); `WebsocketSession.get_config` falls back to
the global config when the name is unknown (`session.py:414-443`).

| Violation              | Behaviour                                                                                                                                                                                     | Coverage                                                                                                                                                 |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Unknown profile name   | Accepted; session keeps the unknown name; config falls back to global. If that thread is resumed later, `resume_thread` raises `ChatProfileUnavailableError` and the thread becomes read-only | **finding F8**; `test_unknown_chat_profile_falls_back_to_global_config` documents it; existing `test_unavailable_chat_profile.py` covers the resume side |
| Invalid `sessionId`    | Regex `[A-Za-z0-9_-]{1,128}` (`session.py:127-133`) -> `ConnectionRefusedError`                                                                                                               | existing `test_session.py:205`                                                                                                                           |
| `userEnv` invalid JSON | `json.loads` raises; connection is refused by the Socket.IO layer                                                                                                                             | not tested here                                                                                                                                          |

### 10. Prompt gallery — `/project/prompts` CRUD

Handlers `server.py:1681-1838`; models `types.py:381-391`. Feature-gated by
`config.features.prompt_gallery`.

| Violation                            | Behaviour                                          | Coverage                                                                                       |
| ------------------------------------ | -------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Feature disabled                     | 400                                                | existing `test_prompt_gallery.py::test_*_feature_disabled`                                     |
| No auth                              | 401                                                | existing `test_prompt_gallery.py::test_list_prompts_no_auth`, `test_add_shared_prompt_no_auth` |
| `title` / `content` missing          | 422                                                | **covered** `test_create_prompt_requires_title_and_content`                                    |
| `title` / `content` blank            | Accepted (200); the save dialog requires non-blank | **finding F9**; `test_create_prompt_rejects_blank_fields` (xfail)                              |
| Update/delete someone else's prompt  | 404                                                | existing `test_update_prompt_not_owner`, `test_delete_prompt_not_found`                        |
| Read/add a prompt that is not shared | 404                                                | existing `test_get_shared_prompt_not_shared`                                                   |
| `isShared` missing                   | 422 (Pydantic)                                     | route model                                                                                    |

## Findings

Severity is impact on correctness/security, not likelihood. Suggested owner is
the Developer unless triage says otherwise.

### F1 — Missing thread returns 500 instead of 404 (medium)

Affected: `GET /project/thread/{id}`, `PUT /project/thread`,
`PUT /project/thread/share`, `DELETE /project/thread`,
`GET /project/thread/{id}/element/{id}`.

`is_thread_author()` (`backend/chainlit/data/acl.py:6-17`) treats a falsy
`get_thread_author()` result as 404, but all built-in layers raise `ValueError`
for an unknown thread (`sql_alchemy.py:205`, `chainlit_data_layer.py:457`,
`dynamodb.py:406`). The exception escapes the route -> 500.

Repro:

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py \
  -k 'missing_thread_returns_404' --runxfail -q
# observed: 500 Internal Server Error for all five routes
```

Expected: 404 for every route. Suggested fix: catch `ValueError` in
`is_thread_author` (or have layers return `""`), and add the tests to CI
without the xfail marker.

### F2 — Invalid session or malformed action/element payload returns 500 (medium)

Affected: `POST /project/action`, `PUT/DELETE /project/element`.

- An unknown `sessionId` reaches `WebsocketSession.require()` and raises
  `ValueError` (500) instead of 404.
- `Action(**payload.action)` raises `TypeError`/`ValidationError` for a
  missing/ill-typed `name` or `payload` (500) instead of 422.
- `_sanitize_custom_element` indexes `id`/`name`/`display` directly (500
  `KeyError`); a non-dict `props` raises a Pydantic validation error (500).

Repro:

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py \
  -k 'action_unknown_session or action_malformed or element_update_unknown_session or element_malformed' \
  --runxfail -q
# observed: 500 for all 11 cases
```

Expected: 404 for an unknown session and 422 for a malformed body.

### F3 — Upload without a multipart Content-Type returns 500 (low)

`upload_file` asserts `file.content_type` (`server.py:3234`). A part with no
`Content-Type` header is valid multipart input, but the `AssertionError`
becomes a 500; `AssertionError` is also removed under `python -O`.

Repro:

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py \
  -k 'upload_without_content_type' --runxfail -q
# observed: 500
```

Expected: 400/422 with a validation message. Fix the same code path as F2
(return an HTTP error instead of asserting).

### F4 — Custom elements trust the client-supplied `threadId` (high, authorisation)

`PUT /project/element` and `DELETE /project/element` authorise the caller
against the _session_ user, then build the element from the request body. The
`threadId` (and `forId`) are copied verbatim into the element
(`server.py:1934-1946`); `Element.remove()` deletes by
`(element.id, element.thread_id)`. A caller with any valid session can
therefore write or delete a custom element in another thread if they know its
id. A shared thread exposes element ids through `GET /project/share/{id}`.

Repro (from the xfail tests):

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py \
  -k 'does_not_trust_client_thread_id' --runxfail -q
# observed: persisted thread_id='victim-thread';
#           deleted ('victim-element', 'victim-thread')
```

Expected: derive `thread_id` from the session, and never pass a
client-supplied thread id to the data layer. Add a regression test without the
xfail marker. Cross-reference the KRE-186 matrix; this is a different endpoint
class from that pass.

### F5 — Missing element returns 200 with a JSON `null` (low)

`get_thread_element` returns `JSONResponse(content=res)` even when
`get_element()` returns `None` (`server.py:1859`). Observed: `200 null`.
Expected: 404. Repro: `-k get_missing_element --runxfail`.

### F6 — Negative page size silently returns the wrong page (low)

`Pagination.first` has no lower bound (`types.py:55-58`). The SQLAlchemy layer
computes `end = start + first` and slices `[start:end]`
(`sql_alchemy.py:407-420`); `first=-1` returns all but the last thread and
sets `hasNextPage=True`. The route accepts the request with 200.

Repro:

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py \
  -k 'negative_page_size' --runxfail -q
# observed: 200 with 4 threads (from 5)
```

Expected: 422. Suggested fix: `Field(ge=1)` (and a sane upper bound) on
`Pagination.first`; add the test without xfail.

### F7 — Socket message content has no server-side validation (low, product decision)

`client_message` accepts empty, whitespace-only and 1 MB `output` values and
invokes `config.code.on_message` with them. The frontend disables the send
button for empty input, but the backend is the only gate for non-browser
clients. There is no limit in code to contradict, so this is recorded as a
silent accept and a product decision, not a crash.

Evidence: `test_socket_empty_and_whitespace_messages_are_accepted`,
`test_socket_oversized_message_is_accepted` (passing, document behaviour).

Suggested options: reject empty/whitespace with an error event; add a
configurable max length; or explicitly document that validation is the app's
responsibility. Do not add a limit without confirming the expected product
behaviour.

### F8 — Unknown chat profile is accepted on connect (low)

`connect` stores any URL-decoded `chatProfile`; an unknown name falls back to
the global config. The thread metadata then carries the unknown profile, so a
later resume reports the profile as unavailable and makes the thread
read-only (`socket.py:120-153`). The user sees a thread that opens read-only
because of a stale/invalid profile name.

Repro: `-k unknown_chat_profile --runxfail` or the passing documentation test
`test_unknown_chat_profile_falls_back_to_global_config`.
Suggested fix: validate the profile against `config.code.set_chat_profiles`
during `connect` and refuse the connection or reset to the default profile.

### F9 — Prompt gallery accepts blank title/content (low)

`CreatePromptRequest`/`UpdatePromptRequest` are plain `str` fields
(`types.py:381-391`); the save dialog blocks blank values client-side.
Observed: `POST /project/prompts` with `title="   "` returns 200. Expected:
422 (strip + `min_length=1`), for create and update.
Repro: `-k create_prompt_rejects_blank --runxfail`.

### F10 — Thread rename accepts an empty name (low)

`UpdateThreadRequest.name` has no length constraint (`types.py:221`); the
rename dialog requires a value. Observed: 200 for `name=""`. Expected: 422.
Repro: `-k rename_thread_rejects_blank --runxfail`.

### F11 — `AskFileSpec.max_files` is not enforced by the server (low)

`AskFileMessage` sends `max_files` (default 1, doc says max 10) and the
frontend limits the picker, but `validate_file_upload` only checks MIME and
size (`server.py:3257-3275`). A direct client can upload more files for the
same ask than the app requested. `SpontaneousFileUploadFeature.max_files`
(default 20) is likewise frontend-only. No test: enforcing this needs
per-ask upload accounting on the session. Suggested fix: count uploaded files
per `ask_parent_id` and reject over-limit uploads with 400.

## 500 classification

Every 500 observed in the in-scope routes is accounted for:

| Route / trigger                                         | Observed                                                                  | Classification                                                                                                       |
| ------------------------------------------------------- | ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Thread routes with a missing thread                     | 500                                                                       | F1                                                                                                                   |
| `POST /project/action` with unknown session             | 500                                                                       | F2                                                                                                                   |
| `POST /project/action` with malformed `action`          | 500                                                                       | F2                                                                                                                   |
| `PUT/DELETE /project/element` with unknown session      | 500                                                                       | F2                                                                                                                   |
| `PUT/DELETE /project/element` with malformed body       | 500                                                                       | F2                                                                                                                   |
| `POST /project/file` without `Content-Type`             | 500                                                                       | F3                                                                                                                   |
| `POST /project/file` without `filename`                 | 500 by the assert; the standard multipart encoding fails earlier with 422 | same code path as F3                                                                                                 |
| `PUT /feedback` without a data layer                    | 500 (`server.py:1293`)                                                    | out of scope; pre-existing inconsistency (DELETE returns 400). Not an input-validation defect; noted for triage only |
| `GET /project/thread` when the data layer itself raises | 500                                                                       | intentional (`server.py:1385-1391`)                                                                                  |
| Prompt/thread data-layer failures                       | 500                                                                       | infrastructure error, not user-input validation                                                                      |
| `validate_file_mime_type` on an invalid operator config | 500 (`assert isinstance(accept, ...)`)                                    | operator misconfiguration, not user input                                                                            |
| Socket malformed payloads                               | no HTTP 500; converted to an `isError` message                            | handled by `process_message`                                                                                         |

## E2E follow-up (browser-only)

These cannot be backend tests because they exercise browser state, the
file-picker, the Engine.IO transport, or the React router. Handed to the E2E
Tester in the linked follow-up issue.

| Case                                               | Why not a backend test                                                                                               |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| Paste a multi-megabyte message and send            | Engine.IO framing/transport limit and composer state live in the browser; the application layer has no limit (F7)    |
| Upload a zero-byte file through the picker         | File picker/React upload progress; backend accepts it (covered by `test_upload_accepts_zero_byte_file`)              |
| Upload a wrong-type file with `accept` configured  | Browser `accept` filtering plus upload error toast; server-side 400 is covered by `test_upload_file_mime_type_check` |
| Drag-and-drop more files than `max_files`          | Dropzone/UI count; the server does not enforce the count (F11)                                                       |
| Double-send (click/Enter twice)                    | Composer debounce and socket ordering; backend accepts both messages                                                 |
| Resume a deleted thread via `/thread/:id`          | React router/data fetching and error UI; backend 404 path is covered (or F1 until fixed)                             |
| Open a deleted/unshared share link `/share/:id`    | Frontend route error state; backend 404 is covered                                                                   |
| Two tabs edit the same message                     | `edit_message` UI state and ordering; backend has no conflict detection                                              |
| Unicode/emoji round-trip in message + thread name  | Rendering and input-method behaviour; backend stores strings verbatim                                                |
| Chat profile removed between selection and connect | UI read-only banner; backend resume path is covered in `test_unavailable_chat_profile.py`                            |
| Whitespace-only message: send button disabled      | UI gate; backend accepts (F7)                                                                                        |

## Verification

```bash
cd backend
uv run pytest tests/test_input_negative_paths.py -q
# 36 passed, 23 xfailed

uv run pytest --cov=chainlit/ -q
# 1360 passed, 23 xfailed, 1 failed (test_cache cold-import timeout, see below)
```

The single suite failure, `test_cache.py::test_cache_nested_and_recursive_calls`,
spawns a subprocess that imports `chainlit` and has a 15 s timeout. On this
runner the first cold import took 16-19 s (MCP/FastAPI imports), then 5-8 s
warm; the test passes on a warm re-run. This is an environment cold-start
flake, not a regression from this branch. The test file's own run is green.

Lint/format/type check are run before the commit; results are in the KRE-187
completion comment.
