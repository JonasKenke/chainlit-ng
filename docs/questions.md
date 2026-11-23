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

## Multiple questions and typed fields

Pass `questions=` to ask several fields in one card. Each field has an `id`,
a `prompt`, and a `type`; the card shows one field at a time with a
`1/4` progress indicator, Back and Next buttons, and a review step that lists
every question with its answer before Submit.

```python
answer = await cl.ask(
    questions=[
        cl.Question("Your name", id="name", type="text", default="Ada"),
        cl.Question("Email", id="email", type="email"),
        cl.Question(
            "Deploy environment",
            id="env",
            type="choice",
            options=[
                cl.QuestionOption("staging", "Staging"),
                cl.QuestionOption("prod", "Production"),
            ],
            default="staging",
            allow_free_text=False,
        ),
        cl.Question("Start date", id="start", type="date"),
        cl.Question("Notes", id="notes", type="text", required=False),
    ]
)

print(answer.answers["email"].text)
print(answer.answers["env"].label)
```

Supported `type` values:

| Type     | Control                                                 | Server validation                               |
| -------- | ------------------------------------------------------- | ----------------------------------------------- |
| `text`   | textarea, or a single-line input with `multiline=False` | plain text rules only                           |
| `email`  | email input                                             | `name@domain.tld` shape, at most 254 characters |
| `date`   | calendar picker                                         | `YYYY-MM-DD` and a real calendar date           |
| `number` | numeric input                                           | finite decimal                                  |
| `url`    | URL input                                               | `http` or `https` scheme with a host            |
| `tel`    | phone input                                             | digits, spaces, `+`, `-`, `.`, `(`, `)`         |
| `choice` | radio list plus optional Other textarea                 | server option id or allowed free text           |

`default` is an option id for `choice` and a typed value otherwise. A
`required=False` field may be left blank; the answer is omitted. The card
validates each field before Next and the review step re-checks every field
before Submit, and the server repeats every check against the ask-time
snapshot.

`cl.ask(question=...)` keeps its original contract. Do not combine it with
`questions=`.

## The answer is untrusted user input

`answer.text` is the user's raw, plain text for the single-question form. For
`questions=`, read `answer.answers[question_id]`; each value has `option_id`,
`label` and `text`. The server trims text, normalizes
line endings, and rejects NUL/C0 controls (except newline and tab), C1 controls,
Unicode bidi controls (`U+061C`, `U+200E`, `U+200F`, `U+202A`–`U+202E`,
`U+2066`–`U+2069`), zero-width characters (`U+200B`–`U+200D`, `U+FEFF`),
invisible tag characters (`U+E0000`–`U+E007F`), and lone UTF-16 surrogates.
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
#   # Added for questions=, in question order:
#   "answers": [
#       {"questionId": "email", "optionId": None, "text": "..."},
#       {"questionId": "env", "optionId": "prod", "text": None},
#   ],
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
  projection replaces it with `{"text": null, "redacted": true}`. For
  `questions=`, each typed item is redacted separately and each option item
  keeps the server id and label. Question defaults are removed from shares
  because they may carry account data. This is a recorded product decision
  (KRE-270 §15.5): there is no disclosure knob in v1.
- The multi-question card validates each field before Next and re-checks every
  field before Submit. The review step lists each prompt with its answer, and a
  Back button returns to the previous field or to the last field from review.

## S1 follow-up dispositions

The question card reuses the S1 approvals substrate. The S1 follow-ups were
disposed of as follows:

- **S1 F1 (share fail-open)** is closed in Q2: `_public_steps` fails closed on
  malformed metadata and unknown interaction kinds.
- **S1 F2 (delete resurrection)** is closed in Q1: the tombstone is re-checked
  inside the step lock, so a late write cannot restore a deleted thread.
- **S1 F5 (`_step_locks` unbounded)** is closed in S2 (`ba3246a9`, KRE-260):
  the lock registry is a `weakref.WeakValueDictionary`, so entries are not
  retained after their last use. The earlier deferral is withdrawn; no
  eviction work remains.

See [Tool-call approvals](approvals.md) for the shared substrate.
