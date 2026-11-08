# Tool-call approvals

Chainlit can pause an async tool call until an authenticated user decides
whether it may run. The server owns the execution arguments and sends them to
the authorized user's live card; the persisted record keeps only a redacted
preview. A decision is accepted only for the user, thread, step and session that
requested it, and a terminal decision must be persisted before the tool can run.

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
- **Edit** – the edited arguments are validated against `approval_schema` on the
  server. The tool runs with the validated arguments, and the step trace shows
  the final redacted arguments. An edit is offered only for a pydantic
  `BaseModel` with `model_config = ConfigDict(extra="forbid")`; otherwise the
  server returns `ApprovalEditNotAllowed`.
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

The raw arguments live in the in-process waiter and in the transient approval
event. They are never written to `steps.input` by the decorated path. If an app
sets `step.input` itself, it owns that persistence decision.

`show_input=False` is a read filter, not a security control. It does not stop
arguments from being stored.

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

## Contract

### Server-held arguments

- The request snapshots the arguments with `deepcopy` and validates that they
  are JSON-compatible. Non-JSON arguments raise `ApprovalInvalidArguments`; they
  are never stringified.
- A decision request carries no arguments. It contains only the session id, the
  approval id, the decision, an optional edit payload and a client request id.
- The approval card and the decision endpoint use the server record. The client
  cannot replace the arguments by editing the payload.
- Approval arguments are keyed by the function signature. For methods, `self`
  and `cls` are excluded from the approval payload and restored for execution.

### Decisions and execution

| Decision                              | Record status | `approved` | Runs? | Arguments                                                      |
| ------------------------------------- | ------------- | ---------- | ----- | -------------------------------------------------------------- |
| approve                               | `approved`    | `True`     | Yes   | Original server-held arguments                                 |
| reject                                | `rejected`    | `False`    | No    | None executed; original arguments remain in the waiter context |
| edit                                  | `edited`      | `True`     | Yes   | Validated edited arguments                                     |
| timeout                               | `expired`     | `False`    | No    | None                                                           |
| stop / replace / delete / session end | `superseded`  | `False`    | No    | None                                                           |
| restart / resume without a waiter     | `orphaned`    | `False`    | No    | None                                                           |

The framework does not auto-approve, remember decisions, or provide an admin
override.

### One pending approval per step

A second request on the same `(thread, step)` supersedes the first. A session
may have at most 8 pending approvals; the 9th raises `ApprovalLimitReached`.

### One run per thread

While a thread has a pending approval, every framework entry point that could
start a run is refused with `ApprovalPending`:

- `client_message`
- `edit_message`
- `audio_end`
- `window_message`
- `config.code.on_message` (covers Slack, Teams, Discord, the scheduler, and
  direct calls)

The pending approval stays pending. `stop` is the explicit cancel path. Tasks
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
revision, tool, title, status, requesting user, timestamps, expiry, edit flag, a
per-approval salt, an HMAC digest of the full arguments, and a recursively
redacted preview. Raw argument values are not part of the persisted record.

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

| Case                                | Outcome                                                                              | Executes?                  |
| ----------------------------------- | ------------------------------------------------------------------------------------ | -------------------------- |
| Approve now                         | Terminal write succeeds, then the waiter resolves                                    | Yes, once                  |
| Reject now                          | `rejected`                                                                           | No                         |
| Edit now                            | Schema-validated final arguments, final redacted trace                               | Yes, with edited arguments |
| Same-session socket reconnect       | The waiter survives; the server does not re-emit the card                            | After a decision, once     |
| Page reload / new session           | No waiter; resume marks a persisted pending record `orphaned`; the app may ask again | No                         |
| Process restart                     | No waiter; resume marks the record `orphaned`                                        | No                         |
| Stop while pending                  | Pending approvals in the thread are superseded before the run is cancelled           | No                         |
| Thread or bulk delete               | Pending approvals are superseded before the rows are deleted                         | No                         |
| Session delete / disconnect timeout | Pending approvals are orphaned                                                       | No                         |
| Duplicate decision retry            | The stored terminal result is returned                                               | No, already decided        |
| Terminal write fails                | Stays `pending`, the waiter is not resolved, a retry is allowed                      | No                         |
| Pending request write fails         | `ApprovalPersistenceError`, no record, no waiter                                     | No                         |
| Decision after expiry               | `expired`; the card is read-only                                                     | No                         |
| Decision on another worker          | No registry entry; cross-worker decisions are not supported                          | No                         |

A stale card can receive the terminal state in the 409 response body, so it can
render the final status without waiting for another event.

## Refusal codes

| Code                       | Typical HTTP status | Meaning                                                                |
| -------------------------- | ------------------- | ---------------------------------------------------------------------- |
| `ApprovalAuthRequired`     | 401                 | No authenticated user                                                  |
| `ApprovalNotReady`         | 409                 | No user message has happened yet                                       |
| `ApprovalUnsupported`      | 400                 | Wrong session type, missing generation, manual flag, or sync decorator |
| `ApprovalThreadMismatch`   | 403                 | The step belongs to another thread                                     |
| `ApprovalSessionMismatch`  | 403                 | The decision comes from another session generation                     |
| `ApprovalNotFound`         | 404                 | Unknown approval, or it does not belong to this thread                 |
| `ApprovalNotPending`       | 409                 | The approval is already terminal or has expired                        |
| `ApprovalPersistenceError` | 500                 | The terminal record could not be proven durable                        |
| `ApprovalInvalidArguments` | 422                 | Arguments are not JSON-compatible, or an edit failed validation        |
| `ApprovalEditNotAllowed`   | 422                 | Edit was requested without an edit schema                              |
| `ApprovalLimitReached`     | 429                 | More than 8 pending approvals in one session                           |
| `ApprovalPending`          | 409                 | A new run was attempted while an approval is pending                   |

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
- **Secrets at rest** – the decorated path stores only a redacted preview. The
  preview redacts case-insensitive key parts such as `authorization`, `api_key`,
  `apikey`, `password`, `secret`, `token` and `credential`, recursively, up to
  depth 8 and 16 KiB. Apps can add keys with `Approval(redact=...)`.
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
