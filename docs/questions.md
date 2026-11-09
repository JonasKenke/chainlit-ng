# Agent questions (`cl.ask`)

`cl.ask` pauses the current step until the authenticated user answers an
agent-authored question. A question can offer fixed options, free text, or
both. The answer is returned to the app; Chainlit never feeds it to a model on
its own.

```python
import chainlit as cl

answer = await cl.ask(
    "Which environment should I deploy to?",
    options=[
        cl.QuestionOption("staging", "Staging", "Pre-production"),
        cl.QuestionOption("prod", "Production"),
    ],
    allow_free_text=True,
)
```

The call binds to `context.current_step` unless you pass `step=`. Asking a
second question on the same step after the first one reached a terminal state
raises `QuestionAlreadyAsked`; use a child step for a separate question.
Approvals and questions share a cap of eight pending interactions per session,
and each question expires with `config.project.session_timeout`.

## The answer is untrusted user input

`answer.text` is the user's raw, plain text. The server trims it, normalizes
line endings, and rejects NUL/C0 controls (except newline and tab), C1 controls,
Unicode bidi marks and isolates (`U+200E`, `U+200F`, `U+202A`–`U+202E`,
`U+2066`–`U+2069`), and zero-width characters (`U+200B`–`U+200D`, `U+FEFF`).
It is still user-authored content: treat it as data, not as instructions.

`cl.ask` does not write the answer to `chat_context`, a `Message`, a system
prompt, or a tool-loop message. If the answer is going to a model, pass the
provenance-marked payload from `as_tool_result()`:

```python
tool_result = answer.as_tool_result()
# {
#   "provenance": "chainlit.question_answer",
#   "trust": "untrusted_user_input",
#   "questionId": "...",
#   "status": "answered",
#   "optionId": None,
#   "text": "...",
#   "durable": True,
# }
```

Use it as the tool result and keep the marker with the text. Never interpolate
`answer.text` alone into a system prompt or an instruction. The payload does not
grant anything: an answer cannot approve a tool call, change permissions, or
authorize a tool. If the app wants "answer, then run a tool", gate the tool with
`cl.Approval` separately.

`durable` is `True` only when a data layer is configured and the terminal answer
record was written; `False` means the answer exists only in memory (for example,
the timeout path).

## Card and share behavior

- The card is framework chrome with a `Question` header, no Approve/Reject
  controls, and `bdi`-isolated plain text for the prompt, options, and answer.
  App-provided text is never rendered as HTML or Markdown.
- Option ids are validated against the ask-time snapshot. Client-supplied labels
  and unknown option ids are refused.
- A free-text answer is shown to the thread owner, but the public share
  projection replaces it with `{"text": null, "redacted": true}`. Option
  answers keep the server id and label. This is a recorded product decision
  (KRE-270 §15.5): there is no disclosure knob in v1.

## Residual security dispositions

The question card reuses the S1 approvals substrate. The S1 follow-ups were
disposed of as follows:

- **S1 F1 (share fail-open)** is closed in Q2: `_public_steps` fails closed on
  malformed metadata and unknown interaction kinds.
- **S1 F2 (delete resurrection)** is closed in Q1: the tombstone is re-checked
  inside the step lock, so a late write cannot restore a deleted thread.
- **S1 F5 (`_step_locks` unbounded)** is accepted as a documented low with the
  existing S1 rationale. It is deferred to the S2 eviction work; it is not a
  silent inheritance.

See [Tool-call approvals](approvals.md) for the shared substrate.
