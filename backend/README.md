<h1 align="center">Chainlit NG</h1>

<p align="center">
  <b>Build production-ready conversational AI applications in Python.</b><br />
  Tool-call approvals, agent questions, observability, and a modern React 19 frontend.
</p>

> **Fork notice:** Chainlit NG is an independent fork of the original [Chainlit](https://github.com/Chainlit/chainlit) project. The upstream project is no longer actively maintained. This line continues development with security fixes and new interaction features.

## Install

```sh
pip install chainlit-ng
```

Requires Python 3.10 or newer.

The Python import is unchanged:

```python
import chainlit as cl
```

The CLI is `chainlit-ng`. The `chainlit` command remains as an alias, so existing scripts and deployment commands keep working.

```sh
chainlit-ng hello
```

## Quickstart

Create `demo.py`:

```python
import chainlit as cl


@cl.step(type="tool")
async def tool():
    await cl.sleep(2)
    return "Response from the tool!"


@cl.on_message
async def main(message: cl.Message):
    tool_res = await tool()
    await cl.Message(content=tool_res).send()
```

Run it with auto-reload:

```sh
chainlit-ng run demo.py -w
```

## What's new in 3.0

- **Tool-call approvals** with schema-generated edit forms and request-changes feedback.
- **`cl.ask` questions** with options, free text, and explicit answer provenance.
- **Thread sharing** with opt-in, read-only grants for named users.
- **Safer MCP**: named servers, a user-server URL allowlist, and per-user OAuth.
- **OpenTelemetry export** with a fixed redaction allowlist.
- **Skills, prompt gallery, and scheduled prompts.**
- **UI improvements**: message timestamps, compact chain-of-thought, side-view by element id, and a configurable upload directory.

Screenshots and the full feature overview are in the [repository README](https://YOUR_GIT_HOST/Kreativhub/chainlit/-/blob/main/README.md).

## Security

Security fixes in this line include the MCP fixes for CVE-2026-45018 and CVE-2026-45019, Azure AD hybrid state and `id_token` verification with a per-login nonce, lone-surrogate request handling, element ownership checks, upload validation, markdown and media hardening, thread-grant ACLs, and OpenTelemetry redaction and export bounds. This is not a claim that every upstream bug is fixed.

See the [MCP advisory](https://YOUR_GIT_HOST/Kreativhub/chainlit/-/blob/main/docs/security-advisory-2026-mcp.md) and the [changelog](https://YOUR_GIT_HOST/Kreativhub/chainlit/-/blob/main/CHANGELOG.md).

## Migrating from Chainlit 2.x

The [3.0 migration guide](https://YOUR_GIT_HOST/Kreativhub/chainlit/-/blob/main/docs/migration-3.0.md) covers the package and CLI rename, the npm package rename, the Recoil to Zustand state change, and breaking configuration updates.

```sh
pip uninstall chainlit
pip install chainlit-ng==3.0.0
```

## Documentation

Guides for approvals, questions, thread sharing, MCP OAuth, telemetry, skills, and scheduled prompts live in the [docs directory](https://YOUR_GIT_HOST/Kreativhub/chainlit/-/tree/main/docs).

## License

Apache License 2.0. Chainlit NG is an independent fork of [Chainlit](https://github.com/Chainlit/chainlit); the original authors and contributors retain credit for the code and documentation they wrote.
