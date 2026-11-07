# Skills

A skill is a Markdown file with a small frontmatter header that ships instructions
for one task. Chainlit discovers skills in `APP_ROOT/skills`, validates them, and
exposes a catalog plus a tool for loading a body on demand. Skills are inert text:
Chainlit never executes, imports, or fetches anything they mention.

## Format

Two layouts are supported:

```text
skills/
  summarize-changelog/
    SKILL.md
  write-release-notes.md
```

Each file starts with `---` frontmatter and a Markdown body:

```markdown
---
name: summarize-changelog
description: Summarize a changelog into user-facing release highlights.
version: 0.1.0
license: MIT
metadata:
  owner: docs-team
---

# Summarize changelog

1. Group entries by type: features, fixes, maintenance.
```

| Field         | Required | Rules                                                           |
| ------------- | -------- | --------------------------------------------------------------- |
| `name`        | yes      | `^[a-z0-9][a-z0-9-]{0,63}$`; unique across the app              |
| `description` | yes      | One line for the catalog; at most 1024 bytes                    |
| `version`     | no       | Free-form string, returned by `get_skill`                       |
| `license`     | no       | Free-form string, returned by `get_skill`                       |
| `metadata`    | no       | Flat `key: value` mapping; YAML block scalars are not supported |

Discovery rules:

- Only `skills/<name>/SKILL.md` and `skills/<name>.md` are read; other files are ignored.
- Hidden entries, symlinks, absolute paths, and `..` traversal are rejected.
- A single invalid skill fails the whole load with `SkillError` instead of being skipped.
- At most 200 skills per app, 64 KiB per body, and 8 KiB of catalog text.
- Duplicate `name` values are rejected; the second file's path is reported.

## Public API

```python
import chainlit as cl
from chainlit.skill import SkillError, load_skills

load_skills(force=False)  # parse + validate, cached per process
cl.list_skills()  # [{"id", "name", "description", "version"}, ...]
cl.get_skill("summarize-changelog")  # metadata + body + path
cl.skills_catalog()  # "name: description" lines, capped at 8 KiB
cl.render_skill("summarize-changelog")  # body wrapped in provenance delimiters
```

`load_skills(force=True)` re-reads the directory, which is useful after the
files change during development. `SkillError` reports invalid files;
`get_skill`/`render_skill` raise `KeyError` for unknown names.

## Progressive disclosure

The point of the registry is to keep bodies out of the context window until the
model asks for one:

1. Put `cl.skills_catalog()` in the system prompt. The model sees only
   `name: description` lines.
2. Register `cl.skill_tool()` with the model provider. It returns
   `{name, description, input_schema, handler}` and the handler is async.
3. When the model calls `load_skill(name)`, the handler returns
   `cl.render_skill(name)`: the body plus `BEGIN`/`END` provenance delimiters
   and the source path. Feed that string back as the tool result.
4. The handler emits a `tool` step named `skill: <name>` by default, so the
   thread shows which skill fired and the loaded body is inspectable.

```python
import chainlit as cl

SYSTEM_PROMPT = "Skills available:\n{catalog}"


@cl.on_chat_start
async def start():
    tool = cl.skill_tool()
    # Example adapter; use the equivalent shape for your provider.
    openai_tools = [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
    ]
    cl.user_session.set("tools", openai_tools)


@cl.on_message
async def on_message(message: cl.Message):
    tool = cl.skill_tool()
    result = await tool["handler"]("summarize-changelog")
    # Send `result` back to the model as the tool output.
    await cl.Message(content=result).send()
```

Handler behaviour:

- Unknown name: returns `Unknown skill '<name>'. Available skills: ...` instead
  of raising, so a hallucinated name cannot crash the app loop.
- Body over the cap: returns an error string and never returns the body. Files
  over the cap are already rejected at load time; this is a second guard.
- Step emission happens inside the handler and needs an active Chainlit session.
  Call the handler from `on_message`, `on_chat_start`, or another request path.

## Refusal rules

The design note for skills (KRE-179 sections 3-5) sets the boundaries:

- **No execution.** A skill is Markdown. Chainlit does not run scripts, shell
  commands, imports, or bundled resources, and neither does the tool helper.
- **No remote fetch.** Skills come only from `APP_ROOT/skills`. URLs, package
  names, and links inside a body are text; Chainlit never downloads them.
- **Caps are enforced.** Skill count, description size, body size, and catalog
  size are validated before a body can reach a model.
- **No per-user scope.** The directory is app-wide; there is no user-specific
  skill store or database table.
- **Treat bodies as untrusted.** A skill body can contain arbitrary instructions.
  Only install skills you would put in a system prompt, and keep provenance
  delimiters intact when passing a body to a model.

## Sample

`backend/chainlit/sample/skills_demo.py` ships two skills and a mock model, so it
runs without API keys:

```bash
cd backend
uv run chainlit run chainlit/sample/skills_demo.py
```

The opening message shows the system prompt with catalog lines only. Send
`summarize-changelog` or `write-release-notes` to trigger `load_skill`; the step
appears in the thread with the loaded body.
