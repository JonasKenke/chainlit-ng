"""Progressive disclosure demo: only the catalog is in context until a skill loads.

Run from ``backend/``:

    uv run chainlit run chainlit/sample/skills_demo.py

Send ``summarize-changelog`` or ``write-release-notes`` to make the mock model call
``load_skill``; any other message answers from the catalog alone.
"""

from pathlib import Path

import chainlit as cl
import chainlit.config as chainlit_config

# Keep the demo skills next to this file so the sample runs from any directory.
chainlit_config.APP_ROOT = str(Path(__file__).resolve().parent)

SYSTEM_PROMPT = """You are a mock model. Only this skills catalog is in your context:

{catalog}

Call load_skill(name) when a catalog entry matches the user's request."""


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("skill_tool", cl.skill_tool())
    await cl.Message(
        content="**System prompt** (catalog only, no skill bodies)\n\n"
        f"```text\n{SYSTEM_PROMPT.format(catalog=cl.skills_catalog())}\n```\n\n"
        "Send `summarize-changelog` or `write-release-notes` to load a skill."
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    tool = cl.user_session.get("skill_tool") or cl.skill_tool()
    names = [skill["name"] for skill in cl.list_skills()]
    name = next(
        (skill_name for skill_name in names if skill_name in message.content), None
    )

    if name is None:
        await cl.Message(
            content="(mock model) No catalog entry matches, so no skill body is in "
            f"context. Available skills: {', '.join(names)}."
        ).send()
        return

    body = await tool["handler"](name)
    await cl.Message(content=f"(mock model) Loaded `{name}`:\n\n{body}").send()
