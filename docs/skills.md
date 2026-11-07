# Skills

A skill is a Markdown file with a small frontmatter header that ships instructions
for one task. Chainlit discovers app skills in `APP_ROOT/skills` and can store
per-user skills in the data layer. Both sources use one parser and one resolver:
a catalog plus a tool for loading a body on demand. Skills are inert text:
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

# Per-user skills use the same resolver; pass the enabled rows in.
user_skills = await cl.load_user_skills()  # empty when the flag is off
cl.skills_catalog(user_skills)
cl.render_skill("my-skill", user_skills)
cl.skill_tool()  # handler loads the current user's enabled skills itself
```

`load_skills(force=True)` re-reads the directory, which is useful after the
files change during development. `SkillError` reports invalid files;
`get_skill`/`render_skill` raise `KeyError` for unknown names. App skills win a
name collision with a per-user skill; the routes reject such a collision when the
user creates or imports the skill.

## Per-user skills

Set `[features].skills = true` to let each authenticated user author skills. The
flag is off by default: no routes are registered, no skill columns are added, and
the prompt gallery behaves exactly as before.

| Method   | Path                          | Purpose                              |
| -------- | ----------------------------- | ------------------------------------ |
| `GET`    | `/project/skills`             | List the current user's skills       |
| `POST`   | `/project/skills`             | Create a skill (disabled by default) |
| `GET`    | `/project/skills/{id}`        | Read one skill                       |
| `PUT`    | `/project/skills/{id}`        | Edit, enable, or disable one skill   |
| `DELETE` | `/project/skills/{id}`        | Delete one skill                     |
| `GET`    | `/project/skills/{id}/export` | Download the skill as Markdown       |
| `POST`   | `/project/skills/import`      | Import Markdown as a disabled skill  |

Every query is filtered by the owning user id, including `get_skill`, `update`,
`delete`, and `export`. A user cannot read another user's skill by id, and import
ignores client-supplied `id`/`userId`/`enabled`: the row always belongs to the
caller and starts disabled.

Imported skills carry provenance in the API payload:

| Field          | Meaning                                            |
| -------------- | -------------------------------------------------- |
| `kind`         | `"skill"`; prompt rows keep `"prompt"`             |
| `userId`       | Owning user (the importer)                         |
| `enabled`      | `false` on import; enabling is an explicit `PUT`   |
| `importedFrom` | Caller-supplied source label for the imported file |
| `importedAt`   | Import timestamp                                   |
| `contentHash`  | SHA-256 of the imported Markdown                   |

`importedFrom` is a label, not a fetch: Chainlit never downloads the source. The
export response includes the same Markdown and an `X-Skill-Content-Hash` header so
a receiver can compare it with `contentHash`. Editing an imported skill keeps its
provenance; the hash describes the version that was imported.

Data layers that do not implement `create_skill`/`list_skills`/`get_skill`/
`update_skill`/`delete_skill` get a clean `501` from the routes. App data layers
stay valid because the base methods are non-abstract.

### Storage and migration

SQLAlchemy stores skills in the existing `prompts` table with a `kind`
discriminator, plus `description`, `enabled`, `importedFrom`, `importedAt`, and
`contentHash` columns. Migration happens lazily on the first skill operation, so
an app with the flag off never changes its schema.

**Up (automatic).** The first skill call inspects the table (`PRAGMA table_info`
on SQLite, `information_schema.columns` on Postgres) and adds only the missing
columns:

```sql
ALTER TABLE prompts ADD COLUMN "kind" TEXT NOT NULL DEFAULT 'prompt';
ALTER TABLE prompts ADD COLUMN "description" TEXT NOT NULL DEFAULT '';
ALTER TABLE prompts ADD COLUMN "enabled" BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE prompts ADD COLUMN "importedFrom" TEXT;
ALTER TABLE prompts ADD COLUMN "importedAt" TEXT;
ALTER TABLE prompts ADD COLUMN "contentHash" TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS prompts_skill_name
  ON prompts ("userId", "title") WHERE "kind" = 'skill';
```

Existing prompt rows get `kind = 'prompt'` and `enabled = false`. The partial
unique index allows one skill name per user.

**Down (manual, no data loss).** Stop using the feature, set
`[features].skills = false`, then remove the index and columns:

```sql
-- Postgres
DROP INDEX IF EXISTS prompts_skill_name;
ALTER TABLE prompts
  DROP COLUMN IF EXISTS "kind",
  DROP COLUMN IF EXISTS "description",
  DROP COLUMN IF EXISTS "enabled",
  DROP COLUMN IF EXISTS "importedFrom",
  DROP COLUMN IF EXISTS "importedAt",
  DROP COLUMN IF EXISTS "contentHash";
```

SQLite before 3.35 cannot drop columns: keep them (prompt queries ignore them)
or rebuild the table, copying `id`, `userId`, `title`, `content`, `isShared`,
`createdAt`, and `updatedAt` and filtering `WHERE "kind" = 'prompt'`.

**DynamoDB and custom layers:** not implemented; the routes return `501` until a
layer implements the five methods.

In no-auth mode (`require_login` false) all anonymous sessions share the
`anonymous` owner, exactly like the prompt gallery. Enable authentication for
real per-user isolation.

## Progressive disclosure

The point of the registry is to keep bodies out of the context window until the
model asks for one:

1. Put `cl.skills_catalog()` in the system prompt. Pass
   `await cl.load_user_skills()` to include the user's enabled skills. The model
   sees only `name: description` lines.
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

- With no argument, the handler loads the current user's enabled skills from the
  data layer on every call, so enable/disable takes effect without a restart.
  Pass `cl.skill_tool(user_skills)` to control the set explicitly.
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
- **No remote fetch.** App skills come only from `APP_ROOT/skills`; per-user
  skills come only from the data layer. URLs, package names, and links inside a
  body are text; Chainlit never downloads them.
- **Caps are enforced.** Skill count, description size, body size, and catalog
  size are validated before a body can reach a model. Per-user skills share the
  same caps; the create and import routes validate with the app-skill parser.
- **Per-user skills are private by default.** There is no automatic sharing or
  marketplace. Imported skills start disabled and keep their provenance; users
  can export a skill as Markdown and import it elsewhere explicitly.
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
