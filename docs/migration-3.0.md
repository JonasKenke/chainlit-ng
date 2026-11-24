# Migrating to Chainlit NG 3.0.0

Chainlit NG 3.0.0 is a rebrand and continuation of the Chainlit 2.x line. The Python API stays source-compatible apart from the items below; the package names, the frontend state library, and some configuration keys changed.

Use this guide if you are upgrading:

- a Python app that depends on `chainlit`, or
- a React app that embeds `@chainlit/react-client`.

## At a glance

| Area                | Before                                   | After                                                      |
| ------------------- | ---------------------------------------- | ---------------------------------------------------------- |
| Python distribution | `chainlit`                               | `chainlit-ng`                                              |
| Python import       | `import chainlit as cl`                  | Unchanged                                                  |
| CLI                 | `chainlit`                               | `chainlit-ng` (the `chainlit` command is kept as an alias) |
| npm client          | `@chainlit/react-client`                 | `@chainlit-ng/react-client`                                |
| Version             | 2.x                                      | 3.0.0                                                      |
| Frontend state      | Recoil atoms and `RecoilRoot`            | Zustand hooks, no state provider                           |
| MCP config          | Per-transport switches                   | Named servers plus a user-server allowlist                 |
| LiteralAI layer     | Bundled data layer and generation models | Removed                                                    |
| `Step.generation`   | LiteralAI generation objects             | Plain `dict`                                               |
| Frontend            | React 18                                 | React 19                                                   |

## 1. Python package and CLI

Uninstall the old distribution before installing the new one. Both provide the `chainlit` import package, so they must not be installed side by side in the same environment.

```sh
pip uninstall chainlit
pip install chainlit-ng==3.0.0
```

The import is unchanged:

```python
import chainlit as cl
```

The CLI binary is now `chainlit-ng`:

```sh
chainlit-ng run app.py -w
```

`chainlit` still works as an alias, so existing scripts, Dockerfiles, and process managers can stay as they are. New deployments should prefer `chainlit-ng`.

Update dependency pins from `chainlit` to `chainlit-ng` and keep extras:

```sh
pip install "chainlit-ng[otel]==3.0.0"
```

If your Dockerfile installs `chainlit` explicitly, change that line and keep the existing `CMD`/entrypoint. If it runs `chainlit run ...`, either command name still works.

## 2. npm package rename

The published React client is now `@chainlit-ng/react-client@3.0.0`.

```sh
npm uninstall @chainlit/react-client
npm install @chainlit-ng/react-client@3.0.0
```

Update every import:

```tsx
import {
  ChainlitAPI,
  ChainlitContext,
  useChatSession
} from '@chainlit-ng/react-client';
```

The private workspace packages (`@chainlit/app` and `@chainlit/copilot`) keep their names; this rename applies to the published client only.

A minimal provider setup now looks like this. There is no `RecoilRoot`:

```tsx
import { ChainlitAPI, ChainlitContext } from '@chainlit-ng/react-client';
import React from 'react';
import ReactDOM from 'react-dom/client';

const apiClient = new ChainlitAPI('http://localhost:8000', 'webapp');

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <ChainlitContext.Provider value={apiClient}>
      <MyApp />
    </ChainlitContext.Provider>
  </React.StrictMode>
);
```

## 3. Recoil to Zustand

The Recoil atoms and the `RecoilRoot` requirement were removed. There is no replacement state provider to add: `ChainlitContext.Provider` is the only wrapper you need.

State is held in a Zustand store scoped to each `ChainlitAPI` instance. Two clients therefore have independent state, and the store is created for the client passed through context.

### Before

```tsx
import { RecoilRoot, useRecoilState } from 'recoil';

import { messagesState } from '@chainlit/react-client';

function Messages() {
  const [messages, setMessages] = useRecoilState(messagesState);

  // ...
}

<ChainlitContext.Provider value={apiClient}>
  <RecoilRoot>
    <Messages />
  </RecoilRoot>
</ChainlitContext.Provider>;
```

### After

```tsx
import { useChainlitState } from '@chainlit-ng/react-client';

function Messages() {
  const [messages, setMessages] = useChainlitState('messages');

  // ...
}

<ChainlitContext.Provider value={apiClient}>
  <Messages />
</ChainlitContext.Provider>;
```

### Hooks

| Hook                         | Purpose                                                        |
| ---------------------------- | -------------------------------------------------------------- |
| `useChainlitStore(selector)` | Read any slice of state and subscribe to a derived value.      |
| `useChainlitState(key)`      | `[value, setValue]` for one state key.                         |
| `useSetChainlitState(key)`   | Setter only, when a component does not need to read the value. |

`useChainlitValue(key)` and `useResetChainlitState(key)` remain available for read-only and reset cases.

Atom imports no longer exist, so map the key you used to the matching state key. For example, `messagesState` becomes `'messages'`, `loadingState` becomes `'loading'`, and `elementsState` becomes `'elements'`.

State added for the new interactions uses these keys:

- `approvals`
- `questions`
- `mcps`
- `skills`
- `prompts`
- `scheduledTasks`

## 4. Breaking configuration changes

### MCP: named servers and a user allowlist

MCP configuration moved from per-transport switches to server-side named servers. The client no longer supplies stdio commands or named-server URLs. Legacy `[features.mcp.sse]`, `[features.mcp.stdio]`, and `[features.mcp.streamable-http]` sections, including `allowed_executables`, abort startup when `features.mcp.enabled = true`. MCP stays off by default.

Replace the old sections with named servers:

```toml
[features.mcp]
enabled = true

[[features.mcp.servers]]
name = "github"
type = "stdio"
command = "npx -y @modelcontextprotocol/server-github"

[[features.mcp.servers]]
name = "internal"
type = "streamable-http"
url = "https://mcp.example.com/mcp"
```

User-provided remote servers require an explicit opt-in and URL allowlist:

```toml
[features.mcp.user_servers]
enabled = true
allowed_urls = ["https://mcp.example.com"]
```

On the client, `connectMcp(sessionId, name)` replaces `connectStdioMCP`, `connectSseMCP`, and `connectStreamableHttpMCP`. User-provided servers use `connectUserMcp(...)`. The security context and full migration table are in the [MCP advisory](security-advisory-2026-mcp.md#migrating-a-211-config).

### Approvals are opt-in

Tool-call approvals are opt-in. Existing steps keep their current behavior unless they pass `requires_approval=True` or the app uses the explicit `cl.Approval` API. Manual `Step(requires_approval=True)` and `metadata={"approval": ...}` are refused; use `@cl.step` or `cl.Approval` instead.

Approvals and questions share a cap of eight pending interactions per session. If your app previously started a tool immediately and never paused for input, review the new card behavior and the [approvals guide](approvals.md).

`cl.Approval(title=...)` was misleading because the tool name is the card heading and the text renders below it. Use `subtitle=...` instead. `title` remains a deprecated alias during the alpha; omit either for no secondary text.

### LiteralAI data layer removed

The LiteralAI data layer, debug UI, and SDK instrumentation helpers were removed. `literalai` is no longer installed transitively; declare it explicitly if your application imports it. Setting `LITERAL_API_KEY` now logs a warning instead of enabling the layer.

Remove `LITERAL_API_KEY` from deployments that no longer use that data layer, and verify the alternative data layer still implements the methods your app needs.

### `Step.generation` is a plain dict

The LiteralAI generation type re-exports were removed. `Step.generation` now stores a plain `dict`. Code that read attributes from a generation object must read keys instead:

```python
# Before: generation was a LiteralAI object
# token_count = step.generation.usage.completion_tokens

# After: generation is a dict
generation = step.generation or {}
usage = generation.get("usage") or {}
token_count = usage.get("completion_tokens")
```

`Step.to_dict()` still serializes objects that implement `to_dict()`, so legacy generation models keep working when persisted.

### React 19 frontend

The bundled frontend runs React 19 and current versions of React-PDF, PDF.js, and ReactPlayer. Install React and React DOM 19 in an app that embeds `@chainlit-ng/react-client`; check the package's `peerDependencies` for the exact supported range.

ReactPlayer 3 dropped support for Dailymotion, SoundCloud, Streamable, Twitch, Facebook, Mixcloud, and Kaltura. Remove those providers from embedded apps or replace them with a supported element type.

## 5. Verification checklist

- [ ] `python -c "import chainlit as cl; print(cl.__version__)"` prints `3.0.0`.
- [ ] `chainlit-ng --version` works, and `chainlit --version` still works as an alias.
- [ ] `chainlit-ng run app.py -w` starts the app and completes a message round trip.
- [ ] `pip check` reports no conflict from a leftover `chainlit` distribution.
- [ ] The React app builds after the `@chainlit-ng/react-client` import rename.
- [ ] `grep -r "from 'recoil'" src` and `grep -r RecoilRoot src` return nothing.
- [ ] State reads use `useChainlitStore`, `useChainlitState`, or `useSetChainlitState`.
- [ ] An approval-enabled step renders a card; approve, reject, edit, and request changes all complete.
- [ ] `cl.ask` returns an answer, and the answer is treated as untrusted input.
- [ ] MCP starts with the named-server config; a legacy MCP config is migrated before enabling it.
- [ ] Code reading `Step.generation` handles a dict.
- [ ] Telemetry, if enabled, exports without including message content, names, inputs, outputs, metadata, elements, or exception messages.
