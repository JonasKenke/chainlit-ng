# Tool-call approvals

Chainlit can pause an async tool call until an authenticated user decides
whether it may run. The server owns the execution arguments and sends only the
non-secret fields to the authorized user's live card; the persisted record keeps
only a redacted preview. A decision is accepted only for the user, thread, step
and session that requested it, and a terminal decision must be persisted before
the tool can run.

Approvals are opt-in. Steps that do not pass `requires_approval` keep their
current behaviour.

## Quick start

```python
import chainlit as cl
from pydantic import BaseModel, ConfigDict


class SendEmailArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str
    subject: str
    body: str
    apiKey: str


@cl.step(type="tool", requires_approval=True, approval_schema=SendEmailArgs)
async def send_email(to: str, subject: str, body: str, apiKey: str) -> str:
    # Re-authorize the final arguments here. The schema checks shape only.
    return "sent"
```

When the step runs:

- **Approve** – the tool runs with the server-held arguments and returns its
  result.
- **Reject** – the tool does not run. The step is marked as an error, the output
  is `Approval rejected`, and the decorated function returns `None`.
- **Edit** – the non-secret edited arguments are validated against
  `approval_schema` on the server. Secret and read-only fields keep their
  original server-held values; a client-supplied value for those fields is
  ignored. The tool runs with the merged arguments, and the step trace shows the
  final redacted arguments. An edit is offered only for a pydantic `BaseModel`
  with `model_config = ConfigDict(extra="forbid")`; otherwise the server returns
  `ApprovalEditNotAllowed`. Pass `allow_edit=False` to remove Edit even when a
  schema is present (`ApprovalActionNotAllowed`).
- **Expire, supersede or orphan** – the tool does not run. The step is marked as
  an error, the output names the terminal state, and the function returns
  `None`.

`requires_approval` is supported only on async functions. A sync function with
the flag raises `ApprovalUnsupported` when the decorator is applied. A
`Step(requires_approval=True)` created directly raises `ApprovalUnsupported` at
`__aenter__` (and `__enter__`); the flag is enforced only through the decorator.

Passing `metadata={"approval": ...}` to `Step` raises `ValueError` because that
key is reserved for the framework record.

### Preview-only `step.input`

For an approval step, the decorator writes only a redacted preview to
`step.input`:

```json
{
  "approval": {
    "tool": "send_email",
    "argumentsPreview": {
      "to": "person@example.com",
      "subject": "Draft",
      "body": "Hello",
      "apiKey": "[redacted]"
    },
    "redacted": true
  }
}
```

The server snapshots the arguments, normalizes them to plain JSON values, and
keeps that snapshot for execution. The live card receives the snapshot; the
persisted `step.input` contains only the redacted preview. Redaction is
key-name based, so values under keys that do not look secret remain visible in
the preview. If an app sets `step.input` itself, it owns that persistence
decision.

`show_input=False` is a read filter, not a security control. It does not stop
arguments from being stored.

The decorator does not expose an extra `redact` list. A tool with a sensitive
field whose name is not caught by the built-in patterns should use the explicit
`Approval(redact=(...))` API.

### Advanced: explicit `Approval`

Use `cl.Approval` when you need a title, a custom timeout, extra redaction keys,
or a decision that is not tied to the decorator:

```python
step = cl.Step(name="wire_transfer", type="tool")
decision = await cl.Approval(
    step=step,
    arguments={"amount": 100, "iban": "..."},
    title="Approve the transfer",
    arguments_schema=TransferArgs,
    redact=("iban",),
    timeout=120,
).ask()

if decision.approved:
    run_transfer(**decision.arguments)
```

The explicit API never runs the tool; the app decides what to do with
`decision.arguments`. Always use those final arguments, not the originals. On
reject, expiry, supersede or orphan, `decision.approved` is `False`.

### Restricting the actions on a card

Both entry points accept `allow_edit` and `allow_request_changes`:

```python
@cl.step(
    type="tool",
    requires_approval=True,
    approval_schema=PublishArgs,
    allow_edit=False,             # Approve / Reject only
    allow_request_changes=False,  # no comment box either
)
async def publish(...):
    ...

await cl.Approval(
    step=step,
    arguments=payload,
    arguments_schema=PublishArgs,
    allow_edit=False,
    allow_request_changes=False,
).ask()
```

| Flag                    | `None` (default)          | `False`                     | `True`                    |
| ----------------------- | ------------------------- | --------------------------- | ------------------------- |
| `allow_edit`            | Edit when a schema exists | No Edit, even with a schema | Edit when a schema exists |
| `allow_request_changes` | Request changes offered   | No Request changes          | Request changes offered   |

The flags are enforced on the server, not only in the card. A decision that the
policy forbids is refused with `ApprovalActionNotAllowed` (HTTP 400) and leaves
the record pending; no arguments or comment are stored. Authorization is checked
first: a wrong user, thread or session generation still gets the usual
`ApprovalNotFound` or `ApprovalSessionMismatch`, so the policy cannot be probed
anonymously.

`allow_edit=True` without an `arguments_schema` does not enable Edit: the server
has no schema to validate against and still returns `ApprovalEditNotAllowed`.
With the decorator, the flags forward to the internal `Approval`. With explicit
`cl.Approval`, they apply directly. Both values are stored on the pending record
(`editAllowed`, `requestChangesAllowed`) and sent in the live payload with the
same names, so the card can hide the buttons. They are preserved on terminal
records and on resume.

### Markdown content without a JSON block

The card never renders raw approval arguments. Send the content as a normal
markdown message, then gate it with a title-only approval:

```python
DRAFT = """# Release notes

- Added approvals
- Added `cl.ask`
"""

await cl.Message(content=DRAFT).send()

async with cl.Step(name="publish_release", type="tool") as step:
    decision = await cl.Approval(
        step=step,
        arguments={},
        title="Publish this document?",
        timeout=120,
    ).ask()

if decision.approved:
    publish()
```

The card then shows only the title and Approve / Reject, directly below the
message. Use this when users should review rendered content rather than edited
JSON; add an `arguments_schema` only when structured edits are required.

### Rich review content

`content` renders markdown inside the card: GFM tables, lists, code, links and
images by URL. It is the only review body; the card does not render the raw
arguments. Provide `content` whenever the approver must see values — the
arguments are execution data, not a display format. Edit opens a form generated
from `arguments_schema` when its fields are simple (see below), with a JSON
editor as the fallback.

```python
async with cl.Step(name="apply_changes", type="tool") as step:
    decision = await cl.Approval(
        step=step,
        arguments={"rows": 3, "target": "production"},
        title="Apply these changes?",
        content="""\
| change | risk |
| --- | --- |
| approvals | low |
| `cl.ask` | medium |
""",
    ).ask()
```

The decorator accepts the same content as a static string:

```python
@cl.step(
    type="tool",
    requires_approval=True,
    approval_content="| change | risk |\n| --- | --- |\n| deploy | low |",
)
async def deploy(...): ...
```

Content is capped at 16 KiB, rendered with HTML escaped unless
`unsafe_allow_html` is enabled, preserved with the persisted approval so it
reappears after a thread resume, and excluded from the public shared-thread
projection. It is display-only: execution still uses the server-held JSON
arguments. After an edit it still describes the requested draft; the
**Original → edited** diff and the final arguments are authoritative.

Set `show_input=False` on the step; otherwise the decorator's redacted argument
preview can render as JSON above the card.

### Editing with a generated form

With `arguments_schema`, the live card renders one input per simple field:
strings, multiline text, integers, numbers, booleans, enums and string lists.
Pydantic field metadata supplies the label, help text and textarea hint:

```python
class TicketDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(title="Title", description="Shown in the ticket list")
    description: str = Field(
        title="Description",
        json_schema_extra={"format": "multiline"},
    )
    priority: Literal["low", "medium", "high"] = "medium"
    assignee: str | None = None
    labels: list[str] = []
```

Schemas with nested models or unsupported field types fall back to the JSON
editor. Editing needs the live arguments; a resumed card only has the redacted
preview, so its Edit action is hidden. `allow_edit=False` hides Edit even on a
live schema-backed card and makes the server refuse the decision.

Some fields are **server-held** and can never be edited from the card:

- names that look like credentials (`api_key`, `apiKey`, `password`, `secret`,
  `token`, `authorization`, `credential`, and other case-insensitive matches),
- JSON-schema `format: "password"` or `writeOnly: true` fields (pydantic
  `SecretStr`),
- fields marked with `json_schema_extra={"readOnly": True}` or the convenience
  alias `json_schema_extra={"editable": False}`.

The live payload omits their values. The form renders a disabled `Server-held`
control instead of the value, and the field is excluded from the edit payload.
On the server, the original values are merged back before schema validation, so
the tool and the argument digest always see the original secret even if a client
supplies a different value. Non-secret fields stay editable. To change a
server-held value, request changes or start a new approval with the new value;
an edit to the card cannot do it.

### Review actions and feedback

| Action          | Effect                                                                                                          |
| --------------- | --------------------------------------------------------------------------------------------------------------- |
| Approve         | Runs with the server-held arguments.                                                                            |
| Edit            | Opens the generated form; saving approves the edited values.                                                    |
| Reject          | Stops execution immediately, without a comment box. Explicit API callers may still attach an optional `reason`. |
| Request changes | Opens a comment box. The comment is required and tells the model what to revise.                                |

Hide an action with `allow_edit=False` or `allow_request_changes=False`; the
server refuses the decision even if a client still sends it. See
[Restricting the actions on a card](#restricting-the-actions-on-a-card).

`request_changes` is a distinct terminal status (`changes_requested`) so the app
can re-plan instead of treating it as a plain rejection:

```python
decision = await cl.Approval(
    step=step,
    arguments=draft.model_dump(),
    arguments_schema=TicketDraft,
    content=render_ticket(draft),
    title="Create this ticket?",
).ask()

if decision.decision == "request_changes":
    await cl.Message(content=f"Changes requested: {decision.comment}").send()
    return
if not decision.approved:
    return
create_ticket(**decision.arguments)
```

Comments are untrusted user input. They are normalized to plain text
(CRLF collapsed, trimmed, newlines and tabs allowed), capped at 500 characters,
and rejected with `ApprovalInvalidArguments` when they contain control, bidi,
zero-width, tag or surrogate characters. The normalized comment is stored as
`reason` on the record and excluded from public shares. The decorator path
records only a fixed status label on the step output (`Approval rejected`,
`Approval changes_requested`, ...), never the comment. Use the explicit
`cl.Approval` API when the model must read the comment and re-plan.

## Contract

### Server-held arguments

- The request snapshots the arguments with `deepcopy` and validates that they
  are JSON-compatible. Non-JSON arguments raise `ApprovalInvalidArguments`; they
  are never stringified.
- A decision request carries no arguments. It contains only the session id, the
  approval id, the decision, an optional edit payload, an optional comment (up
  to 500 characters), and a client request id.
- The approval card and the decision endpoint use the server record. The client
  cannot replace the arguments by editing the payload.
- For a schema-backed approval, secret and read-only fields are server-held:
  their values are omitted from the live `arguments`, their edit descriptor
  carries `serverHeld: true`, and the form renders a disabled `Server-held`
  control. An edit request that supplies a value for one is ignored; the server
  restores the original snapshot value and only then validates the schema and
  computes the digest and preview. Invalid non-secret fields still fail closed.
- Approvals without an `arguments_schema` cannot be edited and still send their
  live arguments to the card. In a JSON-fallback schema, top-level secret-like
  keys are withheld, but nested values inside lists or dictionaries are only
  redacted in the persisted preview.
- Approval arguments are keyed by the function signature. For methods, `self`
  and `cls` are excluded from the approval payload and restored for execution.

### Decisions and execution

| Decision                              | Record status       | `approved` | Runs? | Arguments                                                   |
| ------------------------------------- | ------------------- | ---------- | ----- | ----------------------------------------------------------- |
| approve                               | `approved`          | `True`     | Yes   | Original server-held arguments                              |
| reject                                | `rejected`          | `False`    | No    | None executed; the optional comment is returned             |
| edit                                  | `edited`            | `True`     | Yes   | Validated non-secret edits with original server-held values |
| request changes                       | `changes_requested` | `False`    | No    | None executed; a comment is required and returned           |
| timeout                               | `expired`           | `False`    | No    | None                                                        |
| stop / replace / delete / session end | `superseded`        | `False`    | No    | None                                                        |
| restart / resume without a waiter     | `orphaned`          | `False`    | No    | None                                                        |

The framework does not auto-approve, remember decisions, or provide an admin
override.

### One pending approval per step

A second request on the same `(thread, step)` supersedes the first. A session
may have at most 8 pending approvals; the 9th raises `ApprovalLimitReached`.

### One run per thread

While a thread has a pending interaction (approval or question), every
framework entry point that could start a run is refused with `ApprovalPending`:

- `client_message`
- `edit_message`
- `audio_end`
- `window_message`
- `config.code.on_message` (covers Slack, Teams, Discord, the scheduler, and
  direct calls)

The pending interaction stays pending. `stop` is the explicit cancel path. Tasks
that an app spawns with `asyncio.create_task` are outside this guard.

### Authorization

A decision is bound to:

- the approval id,
- the thread id,
- the step id,
- the authenticated user identifier,
- the session generation.

The user must be the user who requested the approval. The server derives the
decider from the authenticated session, never from the request body. A
different thread, user or session generation, an unknown approval, or a terminal
record is refused without changing state.

## Persistence contract

The record lives under `step.metadata["approval"]`. It contains the approval id,
revision, tool, title, status, requesting user, timestamps, expiry, the two
action flags (`editAllowed`, `requestChangesAllowed`), a per-approval salt, an
HMAC digest of the full arguments, a recursively redacted preview, and the
validated decision comment (`reason`) when one was supplied.
The comment is untrusted user input and stays owner-only. The preview
is key-name based: a value under a non-secret-looking key stays in the record
even though the full-argument digest also covers it. Raw argument objects are
never stored. Public shares keep only the fields in the approval allowlist, so
`reason`, `content`, the previews and the digest stay owner-only.

Writes for an approval-bearing step are serialized by a per-step lock and
awaited while the step is approval-managed. A terminal decision is written
before the waiting coroutine is resumed.

### Custom data layers

`BaseDataLayer.write_approval` is a concrete method, not a new abstract method.
Existing custom layers keep working. The default implementation:

1. reads the thread and step,
2. merges only the `approval` key into the step metadata,
3. checks `expected_revision` (and that a different pending approval does not
   replace this one),
4. calls `update_step`,
5. reads the thread back and requires the normalized record to match.

It returns `True` only when the record is proven durable. Exceptions, a normal
return from `update_step`, or a read-back mismatch are all failures.

Layers with a conditional write should override `write_approval` and return
`True` only after the database confirms the condition and the row. The built-in
SQLAlchemy, DynamoDB and Chainlit data layers do this with rowcount, a
conditional expression, or a returned row.

When no data layer is configured, approvals work in process. There is no
durability claim and no restart/resume guarantee.

Metadata may be a mapping or a JSON string, depending on the layer and database.
The framework normalizes both at read time; a value that is not a mapping is
treated as absent, never as pending.

## Resume and lifecycle

| Case                                | Outcome                                                                                          | Executes?                  |
| ----------------------------------- | ------------------------------------------------------------------------------------------------ | -------------------------- |
| Approve now                         | Terminal write succeeds, then the waiter resolves                                                | Yes, once                  |
| Reject now                          | `rejected`                                                                                       | No                         |
| Edit now                            | Schema-validated non-secret edits plus original server-held fields; final redacted trace         | Yes, with edited arguments |
| Same-session socket reconnect       | The waiter survives; the server does not re-emit the card, so Edit is hidden until a fresh event | After a decision, once     |
| Page reload / new session           | No waiter; resume marks a persisted pending record `orphaned`; the app may ask again             | No                         |
| Process restart                     | No waiter; resume marks the record `orphaned`                                                    | No                         |
| Stop while pending                  | Pending approvals in the thread are superseded before the run is cancelled                       | No                         |
| Thread or bulk delete               | Pending approvals are superseded before the rows are deleted                                     | No                         |
| Session delete / disconnect timeout | Pending approvals are orphaned                                                                   | No                         |
| Duplicate decision retry            | The stored terminal result is returned                                                           | No, already decided        |
| Terminal write fails                | Stays `pending`, the waiter is not resolved, a retry is allowed                                  | No                         |
| Pending request write fails         | `ApprovalPersistenceError`, no record, no waiter                                                 | No                         |
| Decision after expiry               | `expired`; the card is read-only                                                                 | No                         |
| Decision on another worker          | No registry entry; cross-worker decisions are not supported                                      | No                         |

A stale card can receive the terminal state in the 409 response body, so it can
render the final status without waiting for another event.

## Refusal codes

| Code                       | Typical HTTP status | Meaning                                                                                                                        |
| -------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `ApprovalAuthRequired`     | 401                 | No authenticated user                                                                                                          |
| `ApprovalNotReady`         | 409                 | No user message has happened yet                                                                                               |
| `ApprovalUnsupported`      | 400                 | Wrong session type, missing generation, manual flag, or sync decorator                                                         |
| `ApprovalThreadMismatch`   | 403                 | The step belongs to another thread                                                                                             |
| `ApprovalSessionMismatch`  | 403                 | The decision comes from another session generation                                                                             |
| `ApprovalNotFound`         | 404                 | Unknown approval, or it does not belong to this thread                                                                         |
| `ApprovalNotPending`       | 409                 | The approval is already terminal or has expired                                                                                |
| `ApprovalPersistenceError` | 500                 | The terminal record could not be proven durable                                                                                |
| `ApprovalInvalidArguments` | 422                 | Arguments are not JSON-compatible, an edit failed validation, or a comment is missing, too long, or contains unsafe characters |
| `ApprovalEditNotAllowed`   | 422                 | Edit was requested without an edit schema                                                                                      |
| `ApprovalActionNotAllowed` | 400                 | The approval's policy forbids the requested decision (`allow_edit=False` or `allow_request_changes=False`)                     |
| `ApprovalLimitReached`     | 429                 | More than 8 pending approvals in one session                                                                                   |
| `ApprovalPending`          | 409                 | A new run was attempted while an approval is pending                                                                           |

Invalid edit responses contain only a field path and an error type. They never
echo the submitted value. Invalid arguments produce no write and no execution.

## Threat model

- **Argument spoofing / TOCTOU** – the server snapshot is the only execution
  source. The decision payload cannot carry arguments. The HMAC digest and salt
  bind the record for correlation; the digest is not a signature and is not
  verified later.
- **Client-held decision replay** – decisions are one-shot, pending-only, and
  guarded by the step lock. A retry returns the stored result; it does not run
  the tool again.
- **Undetected write failure** – the terminal record must be proven durable
  before the waiter resolves. A swallowed database error is not success.
- **Lost or clobbered record** – approval writes merge the reserved key under a
  per-step lock with a revision check. An app that writes another `Step` object
  with the same id is outside this protection.
- **Secrets in transit and at rest** – a schema-backed approval omits secret and
  read-only field values from the live card, and an edit cannot replace them.
  The decorated path stores only a redacted preview. The preview redacts
  case-insensitive key parts such as `authorization`, `api_key`, `apikey`,
  `password`, `secret`, `token` and `credential`, recursively, up to depth 8 and
  16 KiB. Values under other keys stay visible; the heuristic does not inspect
  values. Apps can add preview keys with `Approval(redact=...)`, or use the
  explicit API when the decorator's fixed list is not enough. An approval
  without a schema, or a JSON-fallback schema with nested secret values, still
  sends those values to the live card by design.
- **Terminal overwrite** – one locked transition handles decisions and timeouts.
  A later transition returns the stored record and changes nothing.
- **Identity / session spoofing** – the record is bound to the authenticated
  user and the session generation. Session deletion orphans the waiter.
- **Second run races the pause** – the shared entry guard refuses new runs while
  an approval is pending.
- **XSS** – the card renders the tool name, title, arguments and diff as plain
  text/JSON. A title is never interpreted as HTML or Markdown.
- **Endpoint confusion** – approval decisions use a dedicated typed route with
  `extra="forbid"`. They never enter `action_callbacks` or `/project/action`.
- **Type confusion on read-back** – SQLite may return metadata as a string,
  Postgres as a mapping. The framework normalizes both and treats a non-mapping
  as absent.
- **Audit tampering** – the record is framework-owned and includes a revision,
  but it is tamper-recording, not tamper-evident. An append-only audit store is
  deferred.

## Adapters

Adapters are not implemented in this release. The mapping is:

- **LangGraph** – `interrupt()` maps to an `Approval`; the decision is the resume
  value.
- **OpenAI Agents SDK** – `needs_approval` maps the tool call to a bound
  `Approval`; the SDK owns continuation.
- **AG-UI** – an interrupt maps to the card event; a decision maps to resume.

An adapter that executes must pass `decision.arguments` (the validated edited
values), never the original arguments.

## Deferred

- Append-only approval audit table and signatures (needs a migration).
- Durable run state, checkpointing and automatic continuation.
- New-session decision handoff and page-reload continuity.
- Cross-worker / distributed approval registry. Sticky sessions and one worker
  are required for the ordering guarantees.
- Admin override, delegated approvers, quorum, approve-on-behalf, remember
  decisions and risk-based auto-approval.
- Free-form editing and per-argument diff; only typed schemas are supported.
- LangGraph, Agents SDK and AG-UI adapters.
- Approval inbox, offline notifications and history export.
- Global rate limiting, secret scanning and at-rest encryption. Per-step and
  per-session caps bound the in-process risk.

## Related

- [Documentation index](README.md)
- [Security advisory for MCP](security-advisory-2026-mcp.md)
