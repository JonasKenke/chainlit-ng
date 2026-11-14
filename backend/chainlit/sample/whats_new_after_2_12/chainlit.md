# Chainlit NG 3.0 demo

This app demonstrates the user-visible changes in the `3.0.0` changelog
section after the 2.12.0 release.

Sign in with **demo / demo**, then click a starter or send a command from the
table below. Send `help` to show the table again in the chat.

| command    | shows                                                                                                 |
| ---------- | ----------------------------------------------------------------------------------------------------- |
| `help`     | this table                                                                                            |
| `approval` | `@cl.step(requires_approval=True)`: email review table, approve / reject / edit, redacted persistence |
| `explicit` | `cl.Approval` with a title, custom redaction, and no schema                                           |
| `ticket`   | typed ticket draft + generated edit form + request changes                                            |
| `markdown` | markdown document + a plain Approve / Reject gate, no JSON                                            |
| `table`    | markdown table rendered inside the approval card, no JSON                                             |
| `ask`      | `cl.ask(...)` with options + free text and `as_tool_result()` provenance                              |
| `media`    | 96x64 PNG sent as bytes, plus `elements_position="above"`                                             |
| `latex`    | LaTeX rendering when `[features] latex = true`; reports the current setting                           |
| `changes`  | the `3.0.0` changelog section in a side panel                                                         |
| `config`   | upload-directory, telemetry, and MCP configuration changes                                            |
| `request`  | `cl.request()` connection context                                                                     |

`cl.ask` and `cl.Approval` need the authenticated session from the login
above. If a tool step waits without showing a card, the served frontend bundle
is probably stale: press **Stop**, rebuild the frontend (see the README), and
reload.
