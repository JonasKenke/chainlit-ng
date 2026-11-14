"""Demo of the user-visible changes that landed after Chainlit 2.12.0.

Run it from this directory so the local ``.chainlit/config.toml`` is used:

    cd backend/chainlit/sample/whats_new_after_2_12
    uv run chainlit run app.py -w

Sign in with ``demo`` / ``demo``. Tool-call approvals and agent questions
require an authenticated websocket session, so the demo uses password auth.

Click a starter or open the readme button (``chainlit.md``) for the command
table, then send a command. ``help`` re-sends the table once the chat has
started. ``changes`` shows the whole ``Unreleased`` changelog section in a side
panel; the other commands exercise the features that need a live session.
"""

import base64
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

import chainlit as cl

# Demo only: the approvals/questions engine needs a JWT secret. Set it before
# the server starts (module import happens before ensure_jwt_secret()).
# PyJWT warns below 32 bytes, so keep this demo secret long enough.
DEMO_AUTH_SECRET = "whats-new-after-2-12-demo-only-jwt-secret"
os.environ.setdefault("CHAINLIT_AUTH_SECRET", DEMO_AUTH_SECRET)

REPO_ROOT = Path(__file__).resolve().parents[4]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# 96x64 RGB gradient sent as bytes: proves binary element content survives
# persistence and resume (no data URL, no file on disk).
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAGAAAABACAIAAABqVuVZAAABf0lEQVR42u3CjWcUAADG4R8l"
    "i7GkOJYsxpLGUaYYyxTHJsVRpjGWFGOTYjSzabM+drZZtabLVeu0bH2clm3V2VKpaVNTU1NZ"
    "aWnZpqamUlP/wfsXvI8H+Ofq4kULLpKy5I+LpC797SLLUn+6yIq0eRcJLP/uIqtWzrlIRuCr"
    "i2Smz7rI2tXTLrJ+zRcXCWZ+dpGNWZ9cZNO6jy6Sm/3BRbYEJ1xk64Z3LhLKeeMihZvHXWRH"
    "7isXCeeNucju/BcusmfbqIuUhJ65yN6CERfZv/2pi5TtHHKRivBjFzm065GLVBY9cJGq4vsu"
    "UlMy4CJ1pUkXObbvros0Huh3keayXhc5VX7bRc4evOUi0cMJF7lQecNFLh+55iKd1V0u0l17"
    "1UVu1l1xkZ6GuIv0nehwkXuRiy4y2BxzkYet513kyZlzLjLc3u4iz6NtLvIydtpFXl9qdZG3"
    "8RYXed/Z5CKTXREXmbp+0kVmEsdd5FtPg4v86K13kV93jrrI32Sti/8BLK6uO7qKLW4AAAAA"
    "SUVORK5CYII="
)

TABLE = """\
| change | risk | owner |
| --- | --- | --- |
| approvals | low | platform |
| `cl.ask` | medium | platform |
| telemetry | low | platform |
"""

DOCUMENT = """\
# Release notes

## Added

- Tool-call approvals with Approve / Reject / Edit
- `cl.ask` for structured questions

## Fixed

- Pending interaction cards stay at the step that created them
"""

HELP = """\
### Chainlit after 2.12.0

| command | shows |
| --- | --- |
| `help` | this table |
| `approval` | `@cl.step(requires_approval=True)`: approve / reject / edit, redacted persistence |
| `explicit` | `cl.Approval` with a title, custom redaction, and no schema |
| `ticket` | typed ticket draft + generated edit form + request changes |
| `markdown` | markdown document + a plain Approve / Reject gate, no JSON |
| `table` | markdown table rendered inside the approval card, no JSON |
| `ask` | `cl.ask(...)` with options + free text and `as_tool_result()` provenance |
| `media` | binary image element + `elements_position` |
| `latex` | LaTeX rendering when `[features] latex = true`: inline `\\(...\\)` and the 254-character closer bound |
| `changes` | the `Unreleased` changelog section in a side panel |
| `config` | upload-directory, telemetry, and MCP configuration changes |
| `request` | `cl.request()` connection context |

Context: `cl.ask` and `cl.Approval` need the login session; use `demo` / `demo`.
If a tool step waits without showing a card, press **Stop** to clear it.
"""


CARD_UI_MARKERS = (
    "approval-card",
    "question-card",
    "Save & approve",
    "Request changes",
)


@lru_cache(maxsize=1)
def _built_ui_has_cards() -> bool:
    """Detect a stale frontend bundle, a common source of "nothing happens"."""
    try:
        from chainlit.server import build_dir

        root = Path(build_dir)
        index = (root / "index.html").read_text(encoding="utf-8")
        match = re.search(r"assets/index-[^\"']+\.js", index)
        if match is None:
            return True
        bundle = (root / match.group(0)).read_text(encoding="utf-8", errors="ignore")
        return all(marker in bundle for marker in CARD_UI_MARKERS)
    except Exception:
        return True


def _unreleased_notes() -> str:
    """Read the repository changelog at runtime so the demo stays current."""
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
        _, _, tail = text.partition("## [Unreleased]")
        notes, separator, _ = tail.partition("\n## [2.12.0]")
        if not separator:
            raise ValueError("release section not found")
        return notes.strip()
    except (OSError, ValueError):
        return "CHANGELOG.md is not reachable from this demo."


class EmailArgs(BaseModel):
    """Schema enables the Edit action; ``extra='forbid'`` is required for it."""

    model_config = ConfigDict(extra="forbid")

    to: str
    subject: str
    body: str
    api_key: str


@cl.step(type="tool", requires_approval=True, approval_schema=EmailArgs)
async def send_email(to: str, subject: str, body: str, api_key: str) -> str:
    # A real tool must re-authorize the final (possibly edited) arguments here.
    return f"sent '{subject}' to {to}"


async def demo_approval() -> None:
    await cl.Message(
        content=(
            "Calling a `@cl.step(requires_approval=True)` tool with an "
            "`approval_schema`. Approve runs it with the server-held "
            "arguments, Reject returns `None`, and Edit validates new JSON. "
            "The persisted step input is a preview, so `api_key` is redacted."
        )
    ).send()
    result = await send_email(
        to="person@example.com",
        subject="Quarterly report",
        body="See attached.",
        api_key="sk-demo-should-not-be-persisted",
    )
    if result is None:
        await cl.Message(
            content="The tool did not run (rejected, expired, or superseded)."
        ).send()
    else:
        await cl.Message(content=f"Tool result: {result}").send()


async def demo_explicit() -> None:
    await cl.Message(
        content=(
            "`cl.Approval` is the explicit API: a custom title and an extra "
            "redaction key. There is no schema, so the card offers no Edit."
        )
    ).send()
    async with cl.Step(name="wire_transfer", type="tool") as step:
        decision = await cl.Approval(
            step=step,
            arguments={
                "amount": 250,
                "iban": "DE89 3704 0044 0532 0130 00",
                "memo": "Rent",
            },
            title="Approve the wire transfer",
            redact=("iban",),
            timeout=120,
        ).ask()
    if decision.decision == "request_changes":
        await cl.Message(
            content=(
                "Changes requested; the model receives "
                f"`decision.comment`: {decision.comment}"
            )
        ).send()
    elif decision.approved:
        await cl.Message(
            content=(
                f"Executed {decision.arguments['memo']} "
                f"for {decision.arguments['amount']}."
            )
        ).send()
    else:
        await cl.Message(content=f"Not executed: {decision.decision}.").send()


class TicketDraft(BaseModel):
    """Schema drives the edit form: labels, enums, and a textarea."""

    model_config = ConfigDict(extra="forbid")

    project: Literal["Platform", "Growth", "Support"]
    title: str = Field(title="Title")
    description: str = Field(
        title="Description",
        json_schema_extra={"format": "multiline"},
    )
    priority: Literal["low", "medium", "high"] = "medium"
    assignee: str | None = None
    labels: list[str] = []


def render_ticket(draft: TicketDraft) -> str:
    return f"""\
### {draft.title}

{draft.description}

| Field | Value |
| --- | --- |
| Project | `{draft.project}` |
| Priority | {draft.priority} |
| Assignee | {draft.assignee or "Unassigned"} |
| Labels | {", ".join(draft.labels) or "—"} |
"""


async def demo_ticket() -> None:
    draft = TicketDraft(
        project="Platform",
        title="Rate limit on /v1/search returns 500",
        description=(
            "Requests to `/v1/search` intermittently return 500 when the "
            "limiter trips. Expected: 429 with `Retry-After`.\n\n"
            "Reproduction steps and logs are attached."
        ),
        priority="high",
        assignee="ana@example.com",
        labels=["api", "reliability"],
    )
    async with cl.Step(name="create_ticket", type="tool", show_input=False) as step:
        decision = await cl.Approval(
            step=step,
            arguments=draft.model_dump(),
            arguments_schema=TicketDraft,
            title="Create this ticket?",
            content=render_ticket(draft),
            timeout=300,
        ).ask()
    if decision.decision == "request_changes":
        await cl.Message(
            content=(
                "Changes requested; the model would revise the draft and ask "
                f"again:\n\n> {decision.comment}"
            )
        ).send()
    elif not decision.approved:
        await cl.Message(content=f"Not created: {decision.decision}.").send()
    else:
        final = TicketDraft.model_validate(decision.arguments)
        await cl.Message(
            content=f"Created CHAIN-1234 in {final.project}: {final.title}"
        ).send()


async def demo_table_approval() -> None:
    async with cl.Step(name="apply_changes", type="tool") as step:
        decision = await cl.Approval(
            step=step,
            arguments={"rows": 3, "target": "production"},
            title="Apply these changes?",
            content=TABLE,
            timeout=120,
        ).ask()
    if decision.approved:
        await cl.Message(content="Changes applied.").send()
    else:
        await cl.Message(content=f"Not applied: {decision.decision}.").send()


async def demo_markdown_approval() -> None:
    await cl.Message(content=DOCUMENT).send()
    async with cl.Step(name="publish_release", type="tool") as step:
        decision = await cl.Approval(
            step=step,
            arguments={},
            title="Publish this document?",
            timeout=120,
        ).ask()
    if decision.approved:
        await cl.Message(content="Document published.").send()
    else:
        await cl.Message(content=f"Not published: {decision.decision}.").send()


async def demo_ask() -> None:
    answer = await cl.ask(
        "Which environment should the deployment target?",
        options=[
            cl.QuestionOption("staging", "Staging", "Pre-production"),
            cl.QuestionOption("production", "Production", "Live traffic"),
        ],
        allow_free_text=True,
        timeout=120,
    )
    if not answer.answered:
        await cl.Message(
            content=f"The question ended without an answer ({answer.status})."
        ).send()
        return

    if answer.label:
        choice = f"You chose the **{answer.label}** option."
    else:
        choice = "You answered with free text."
    payload = json.dumps(answer.as_tool_result(), indent=2)
    await cl.Message(
        content=(
            f"{choice}\n\n"
            "Answers are untrusted user input. A model should receive only "
            "this provenance-marked payload:\n"
            f"```json\n{payload}\n```"
        )
    ).send()


async def demo_media() -> None:
    image = cl.Image(name="resume-check.png", content=PNG_BYTES, display="inline")
    await cl.Message(
        content=(
            "`elements_position='above'` puts this inline element above the "
            "text. The image is sent as bytes, not a URL; with a data layer "
            "configured it must still render after a reload (thread resume)."
        ),
        elements=[image],
        elements_position="above",
    ).send()


async def demo_latex() -> None:
    from chainlit.config import config

    if not config.features.latex:
        await cl.Message(
            content=(
                "LaTeX rendering is disabled (`latex = false`). Set "
                "`latex = true` under `[features]` in `.chainlit/config.toml` "
                "and restart the app to render inline `\\(...\\)` and block "
                "`\\[...\\]` math."
            )
        ).send()
        return
    await cl.Message(
        content=(
            "LaTeX rendering is enabled (`latex = true`).\n\n"
            "Inline `\\(...\\)` renders: \\(R = V / I\\)\n\n"
            "\\[y = ax^2 + bx + c\\]\n\n"
            "A closer more than ~254 characters away stays literal:\n\n"
            f"\\({'x' * 300}\\)"
        )
    ).send()


async def demo_help() -> None:
    await cl.Message(content=HELP).send()


async def demo_changes() -> None:
    details = cl.Text(
        name="Unreleased changes",
        content=_unreleased_notes(),
        display="side",
        auto_expand=False,
    )
    await cl.Message(
        content=(
            "The `Unreleased` changelog is sent with `auto_expand=False`, then "
            "opened explicitly by element id with `cl.open_side_view()`."
        ),
        elements=[details],
    ).send()
    await cl.open_side_view(details.id)


async def demo_config() -> None:
    from chainlit.config import FILES_DIRECTORY, config

    uploads = config.features.spontaneous_file_upload
    telemetry = config.features.telemetry
    configured = uploads.directory if uploads else None
    await cl.Message(
        content=(
            "**Upload directory** (`[features.spontaneous_file_upload]`)\n\n"
            f"- configured: `{configured}`\n"
            f"- resolved: `{FILES_DIRECTORY}`\n"
            "- a configured directory is preserved on shutdown; the legacy "
            "default `.files` is removed\n\n"
            "**OpenTelemetry** (`[features.telemetry] enabled = true`, "
            "extra `chainlit[otel]`)\n\n"
            f"- enabled: `{telemetry.enabled}`\n\n"
            "**MCP** (breaking config change)\n\n"
            "- servers are named under `[[features.mcp.servers]]`\n"
            "- user-provided URLs require `[features.mcp.user_servers]` with "
            "`enabled = true` and `allowed_urls`\n"
            "- see `docs/security-advisory-2026-mcp.md`"
        )
    ).send()


async def demo_request() -> None:
    request = cl.request()
    if request is None:
        await cl.Message(content="No web request context here.").send()
        return
    client = request.client.host if request.client else "unknown"
    await cl.Message(
        content=(
            "`cl.request()` exposes the connection request:\n"
            f"- path: `{request.url.path}`\n"
            f"- client: `{client}`\n"
            f"- user-agent: `{request.headers.get('user-agent', '')}`"
        )
    ).send()


COMMANDS = {
    "help": demo_help,
    "approval": demo_approval,
    "explicit": demo_explicit,
    "ticket": demo_ticket,
    "markdown": demo_markdown_approval,
    "table": demo_table_approval,
    "ask": demo_ask,
    "media": demo_media,
    "latex": demo_latex,
    "changes": demo_changes,
    "config": demo_config,
    "request": demo_request,
}


@cl.password_auth_callback
async def authenticate(username: str, password: str) -> cl.User | None:
    if username == "demo" and password == "demo":
        return cl.User(identifier="demo", display_name="Demo user")
    return None


@cl.set_starters
async def starters(_user: cl.User | None = None) -> list[cl.Starter]:
    return [
        cl.Starter(label="Approve a tool", message="approval"),
        cl.Starter(label="Create a ticket", message="ticket"),
        cl.Starter(label="Markdown approval", message="markdown"),
        cl.Starter(label="Table approval", message="table"),
        cl.Starter(label="Agent question", message="ask"),
        cl.Starter(label="Resume media", message="media"),
        cl.Starter(label="All changes", message="changes"),
    ]


@cl.on_chat_start
async def on_chat_start() -> None:
    # Console only: a message here makes WelcomeScreen return null and hides
    # every starter. The command table lives in chainlit.md and `help`.
    print("Chainlit demo login: demo / demo", flush=True)
    if not _built_ui_has_cards():
        warning = (
            "**The served frontend is stale.** Its bundle predates the "
            "approvals and questions UI, so those cards cannot render and the "
            "backend step waits forever. From the repository root run:\n\n"
            "```bash\n"
            "pnpm --filter @chainlit/react-client build\n"
            "pnpm --filter @chainlit/app build\n"
            "```\n\n"
            "Then reload this page. If a step is already waiting, press "
            "**Stop** first to clear the pending approval."
        )
        print(warning.replace("**", ""), flush=True)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    if message.elements:
        names = ", ".join(element.name for element in message.elements)
        await cl.Message(
            content=(
                f"Upload received: {names}. The configured "
                "`[features.spontaneous_file_upload] directory` keeps the file "
                "on disk after shutdown."
            )
        ).send()
        return

    command = message.content.strip().lower()
    handler = COMMANDS.get(command)
    if handler is None:
        await cl.Message(content=f"Unknown command `{command}`.\n\n{HELP}").send()
        return
    await handler()
