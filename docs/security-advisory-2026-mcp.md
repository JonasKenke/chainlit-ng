# Security advisory: MCP endpoint vulnerabilities

**Fork advisory date**: 2026-11-07
**Upstream advisory**: SPL-2026-001 / SPL-2026-002 (Chainlit `v2.12.0`)
**CVE**: CVE-2026-45018 (SPL-2026-001) / CVE-2026-45019 (SPL-2026-002)
**Severity**: Critical (CVSS v3.1: 9.8) / High (CVSS v3.1: 7.2)
**Affected versions**: upstream `v2.4.0rc0` – `v2.11.x`; fork `2.12.0` builds before merge `3320cae9`. Both only with `features.mcp.enabled = true`.
**Fixed in**: upstream `v2.12.0` (release tag `2.12.0`, commit `0565fd0e`); fork `main` since merge `3320cae9` (next release; see `CHANGELOG.md`)
**Default**: MCP is off by default (`features.mcp.enabled = false`)

> **Fork note.** This advisory is ported from the upstream Chainlit advisory. The fork
> advertised `2.12.0` before it contained these fixes, so treat every fork `2.12.0` build
> before merge `3320cae9` as affected when MCP is enabled. The fixes are in the fork's
> 2.12.0 port: backend `fa281faa` (named servers, client-supplied stdio removed) and
> `b52ecd05` (user-server allowlist and SSRF guards), client `4828cbca`.
>
> MCP was kept disabled by default throughout the port, and it remains disabled by
> default after it. Enabling MCP now requires the new configuration schema described in
> [Migrating a 2.11 config](#migrating-a-211-config); a legacy config aborts startup when
> MCP is enabled.

---

## Summary

Two vulnerabilities were identified in Chainlit's `/mcp` endpoint, which handles Model Context
Protocol connectivity. Both require `features.mcp.enabled = true` to be exploitable.

| ID           | CVE            | Title                              | CVSS v3.1 | Severity |
| ------------ | -------------- | ---------------------------------- | --------- | -------- |
| SPL-2026-001 | CVE-2026-45018 | Command Injection via MCP stdio    | 9.8       | Critical |
| SPL-2026-002 | CVE-2026-45019 | SSRF via MCP streamable-http / SSE | 7.2       | High     |

---

## SPL-2026-001 / CVE-2026-45018 — Command Injection via MCP stdio

### Description

The `POST /mcp` endpoint accepted a user-supplied `fullCommand` string when `clientType` was
`"stdio"`. This string was validated only by checking the executable name against a configurable
allowlist (`allowed_executables`) — arguments were passed through unchecked.

Because `npx` (the default allowed executable) supports `-c` for arbitrary shell execution,
an attacker could send:

```json
{
  "sessionId": "<valid session>",
  "clientType": "stdio",
  "fullCommand": "npx -y -c 'id > /tmp/pwned'"
}
```

The server spawned the subprocess before the MCP handshake completed, so the shell command
executed even though the MCP connection subsequently failed. This gave an unauthenticated
attacker code execution on the host running Chainlit.

**Root cause**: The design placed subprocess command construction in client-controlled input.
No argument-level filtering can safely sandbox this because the attacker controls the full
argument list.

### Impact

An unauthenticated remote attacker can execute arbitrary shell commands on the Chainlit server
with the privileges of the Chainlit process. This allows reading and writing files, exfiltrating
secrets, installing backdoors, and pivoting into internal networks.

### Affected configuration

Any Chainlit deployment with:

```toml
[features.mcp]
enabled = true
```

MCP is disabled by default (`enabled = false`).

---

## SPL-2026-002 / CVE-2026-45019 — SSRF via MCP streamable-http / SSE

### Description

The `POST /mcp` endpoint accepted arbitrary `url` and `headers` from the client when
`clientType` was `"sse"` or `"streamable-http"`. The URL was passed directly to the MCP SDK's
HTTP client with no scheme check, no hostname validation, and no allowlist. The attacker also
controlled all HTTP headers, enabling header injection attacks.

An attacker could force the Chainlit server to make outbound HTTP requests to internal
services (cloud metadata endpoints, internal APIs, databases) and forge sensitive headers
(`Authorization`, `Cookie`, `X-Internal-Secret`) in those requests:

```json
{
  "sessionId": "<valid session>",
  "clientType": "streamable-http",
  "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
  "headers": { "Authorization": "Bearer attacker-controlled-token" }
}
```

This is a blind SSRF — response bodies are consumed by the MCP client and not returned to
the attacker — but it enables service discovery, port scanning, metadata endpoint probing,
and side-effect attacks via header injection.

### Impact

An unauthenticated remote attacker can cause the Chainlit server to make arbitrary outbound
HTTP requests to internal network hosts, forge HTTP headers on those requests, and probe
cloud instance metadata services.

---

## Fix

### SPL-2026-001 (Critical) — Architectural fix

The `fullCommand` field has been **removed from the client request entirely**. stdio MCP
servers are now defined exclusively in server-side configuration (`.chainlit/config.toml`).
The client sends only a `name` to activate a pre-configured server:

**Before (`v2.11.x`, vulnerable):**

```json
{
  "sessionId": "...",
  "clientType": "stdio",
  "fullCommand": "npx -y @mcp/github"
}
```

**After (`v2.12.0`, fixed):**

```toml
# .chainlit/config.toml (developer-controlled)
[[features.mcp.servers]]
name = "github"
type = "stdio"
command = "npx -y @modelcontextprotocol/server-github"
```

```json
{ "sessionId": "...", "name": "github" }
```

The server looks up the command from its own config and spawns the subprocess. The client
cannot influence the command string in any way.

### SPL-2026-002 (High) — Defence-in-depth improvements

User-provided SSE/HTTP connections now require an explicit opt-in (`user_servers.enabled = true`)
and a URL allowlist. Additionally:

- URL scheme enforced to `http`/`https` only
- URL allowlist uses parsed-component comparison (prevents subdomain/userinfo bypass)
- Path prefix matching normalised to prevent sibling-path bypass (e.g. `/v1-evil` no longer
  matches an allowlist entry of `/v1`)
- Dangerous HTTP headers (`Cookie`, `Host`, `X-Forwarded-*`, `Proxy-Authorization`, etc.)
  are stripped before forwarding
- **HTTP redirects are not followed, for developer-configured ("named") servers as well as
  user-provided ones.** The MCP Python SDK hardcodes `follow_redirects=True` on the httpx
  client it builds internally, which means only the _first_ hop of a connection would ever be
  checked against our destination allowlist (user-provided servers) or origin pin (named
  servers) — a permitted server could 302 the connection anywhere. httpx forwards every
  request header except `Authorization` and `Cookie` to a redirect target, so a configured API
  key carried in a custom header could leak to the redirect destination. `mcp.py`'s
  `make_mcp_http_client_factory` overrides this with `follow_redirects=False` on the client it
  hands to both transports, and re-validates every outgoing request (not just the initial URL)
  against the same `check_destination` hook. That last part also closes a related gap: the SSE
  transport takes its POST target from the server's own `endpoint` event at runtime, and the
  SDK accepts any same-origin value for it — including one outside the path subtree an
  allowlist entry granted. That a configured server's own runtime instructions can move its
  destination this way is the reason named servers get the same redirect and
  destination-recheck treatment as user-provided ones: the trust boundary is identical, so
  hardening only one side would have been inconsistent. Deployments that relied on a redirect
  to reach their real MCP endpoint should configure the final `https://` URL directly.

**Recommended configuration (opt-in user servers):**

```toml
[features.mcp.user_servers]
enabled = true
allowed_urls = ["https://mcp.example.com"]  # only permit specific origins
```

---

## Migrating a 2.11 config

The 2.12.0 schema replaces the per-transport switches with named servers and an explicit
user-server allowlist. The migration is required only before **enabling** MCP. With
`features.mcp.enabled = false`, legacy sections are ignored with a warning; with
`enabled = true`, startup aborts and names every legacy key it found.

### 1. Remove the legacy transport sections

A 2.11 config typically looked like this:

```toml
[features.mcp]
enabled = true

[features.mcp.sse]
enabled = true

[features.mcp.streamable-http]
enabled = true

[features.mcp.stdio]
enabled = true
allowed_executables = ["npx", "uvx"]
```

In 2.11 these switches only enabled transports; the actual command or URL came from the
browser. In 2.12.0 the server list is developer-controlled, and the legacy sections
(including `allowed_executables`) are rejected when MCP is enabled.

### 2. Declare named stdio servers

**Before:** the user picked stdio in the MCP dialog and typed a command such as
`npx -y @modelcontextprotocol/server-github`.

**After:** declare the command in config; the client sends only the name.

```toml
[[features.mcp.servers]]
name = "github"
type = "stdio"
command = "npx -y @modelcontextprotocol/server-github"
# Optional, merged over the SDK's default environment at spawn time.
# Never exposed to clients.
# env = { GITHUB_TOKEN = "..." }
```

```ts
await api.connectMcp(sessionId, 'github');
```

### 3. Declare named SSE / streamable-http servers

**Before:** the user picked SSE or streamable HTTP and typed the URL and headers.

**After:** declare the transport and URL in config. Headers stay server-side; the browser
never receives them.

```toml
[[features.mcp.servers]]
name = "internal"
type = "streamable-http" # or "sse"
url = "https://mcp.example.com/mcp"
# Optional, server-side only.
# headers = { Authorization = "Bearer ..." }
```

```ts
await api.connectMcp(sessionId, 'internal');
```

### 4. Allow user-provided servers explicitly

**Before:** any user-supplied SSE/HTTP URL was accepted.

**After:** user-provided servers need an opt-in and an allowlist. `stdio` can never be
user-provided.

```toml
[features.mcp.user_servers]
enabled = true
allowed_urls = ["https://mcp.example.com"]
```

- An empty `allowed_urls` denies every user-provided server.
- Entries match scheme, hostname and effective port, plus an optional path subtree
  (`https://mcp.example.com/team` allows only that subtree). A leading `*.` host
  label matches subdomains (not the apex), and `*` in a path stays within one
  segment, though the matched segment's sub-tree is still allowed as for a
  literal path; wildcards in scheme, port, credentials, query or fragment are
  config errors.
- Only `http`/`https` schemes are accepted; prefer `https` for remote servers.
- Per-user OAuth (streamable HTTP only) requires `https` outside loopback; see
  [Per-user OAuth for MCP connections](mcp-oauth.md).
- Dangerous request headers are stripped, and redirects are not followed.

```ts
await api.connectUserMcp(
  sessionId,
  name,
  'streamable-http',
  url,
  headers // optional
);
```

For per-user OAuth against a user-provided streamable-HTTP server, pass `useOAuth` as the
last argument. See [Per-user OAuth for MCP connections](mcp-oauth.md).

### 5. Client library changes

The next `@chainlit/react-client` release ships `0.7.0` for this port (the manifest is still `0.6.0`).

| 2.11 method                                                             | 2.12.0 replacement                                                                |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------- |
| `connectStdioMCP(sessionId, name, fullCommand)`                         | `connectMcp(sessionId, name)` for a server declared in `[[features.mcp.servers]]` |
| `connectSseMCP(sessionId, name, url, headers?)`                         | `connectMcp(...)` for a named server, or `connectUserMcp(...)` for a user server  |
| `connectStreamableHttpMCP(sessionId, name, url, headers?)`              | `connectMcp(...)` for a named server, or `connectUserMcp(...)` for a user server  |
| `connectUserMcp(sessionId, name, type, url, headers?, useOAuth?)` (new) | User-provided SSE/streamable-http only; `useOAuth` requires streamable HTTP       |

Stored MCP entries are migrated to the named-server shape on load. Headers, tools and the
authorization URL are never persisted; URLs are sanitized before storage.

---

## Mitigation for users who cannot upgrade immediately

If upgrading to the fork 2.12.0 port is not immediately possible:

1. **Disable MCP entirely** — set `features.mcp.enabled = false` in your config (this is
   the default). This fully prevents exploitation of both vulnerabilities.

2. **Restrict network access** — place the Chainlit server behind a firewall that prevents
   outbound connections to internal networks and cloud metadata endpoints.

3. **Enable authentication** — configure `@cl.password_auth_callback`, `@cl.header_auth_callback`,
   or OAuth so that the `/mcp` endpoint requires a valid user session. Anonymous access
   significantly lowers the bar for exploitation.

---

## Credit

Reported by Vipin and Stephen at SPL Security (security@spl.team) under coordinated disclosure.
The report included working proof-of-concept exploits for both vulnerabilities tested against
Chainlit v2.11.0. We thank SPL Security for the thorough, responsibly disclosed report.

---

## Timeline

| Date       | Event                                                                   |
| ---------- | ----------------------------------------------------------------------- |
| 2026-04-08 | Report received from SPL Security and confirmed by upstream maintainers |
| 2026-11-07 | Fork port merged (PR #20, `3320cae9`); this fork advisory published     |
