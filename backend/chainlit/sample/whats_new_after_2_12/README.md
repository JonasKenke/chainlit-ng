# Chainlit after 2.12.0 — demo

One small app for the user-visible features and fixes in the `Unreleased`
section of the repository changelog. The `changes` command reads that section
at runtime and shows it in a side panel.

## Run

The backend serves `frontend/dist` when it exists. A bundle built before the
approvals/questions UI cannot render those cards, and the backend step then
waits forever. On chat start the app checks the served bundle for the approval
and question card UI plus the **Save & approve** / **Request changes** actions;
if any marker is missing it prints a rebuild warning to the console only, so
the welcome screen and its starters still appear. If `frontend/dist` is stale,
rebuild it first:

```bash
# from the repository root
pnpm --filter @chainlit/react-client build
pnpm --filter @chainlit/app build
```

Then start the demo from this directory:

```bash
cd backend/chainlit/sample/whats_new_after_2_12
uv run chainlit run app.py -w
```

Open http://localhost:8000 and sign in with `demo` / `demo` (the demo uses
password auth so approvals and questions have an authenticated session). The
welcome screen shows the starters; the readme button opens `chainlit.md` with
the full command table. Nothing is sent to the chat on chat start. Send `help`
at any time to print that table in the conversation.

If a tool step is already waiting and no approval card is visible, press
**Stop** in the message composer. Stop supersedes the pending approval; then
rebuild the frontend if needed and try the command again. Restarting the app
also clears pending approvals (they are in memory only).

## Commands

| command    | feature                                                                           |
| ---------- | --------------------------------------------------------------------------------- |
| `help`     | re-send the command table in the chat                                             |
| `approval` | decorator-gated tool: email review table, approve / reject / edit, redacted trace |
| `explicit` | `cl.Approval` with title and custom redaction                                     |
| `ticket`   | typed ticket draft + generated form + request changes                             |
| `markdown` | markdown message + title-only approval, no JSON block                             |
| `table`    | markdown table inside the approval card, no JSON block                            |
| `ask`      | `cl.ask` options + free text, `as_tool_result()` provenance                       |
| `media`    | visible 96x64 PNG sent as bytes, `elements_position="above"`                      |
| `latex`    | reports `[features] latex`; demo of inline math when enabled                      |
| `changes`  | full `Unreleased` changelog in a side panel                                       |
| `config`   | upload-directory, telemetry, and MCP config changes                               |
| `request`  | `cl.request()` connection context                                                 |

`latex` is enabled in this sample's `.chainlit/config.toml`
(`latex = true`). With `latex = false` the command says rendering is disabled
and how to enable it instead of claiming that math renders.

The demo has no data layer, so approvals/questions exist only in memory and
`QuestionAnswer.durable` is `false`; add a data layer to exercise persistence
and thread resume. Pending approval and question cards render inline at their step, between
the user's input and the assistant's follow-up.
