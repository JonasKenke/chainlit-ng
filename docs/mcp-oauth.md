# Per-user OAuth for MCP connections

Chainlit can connect a user-provided MCP server with OAuth on behalf of the
signed-in user. Each user authorizes their own account at the server's
authorization server; access and refresh tokens stay on the server, keyed by the
user and the MCP server, and are never shared between users.

This flow is for user-provided servers (`[features.mcp.user_servers]`) that use
the **streamable HTTP** transport. Developer-configured servers
(`[[features.mcp.servers]]`) keep using their configured headers and do not use
per-user OAuth. `useOAuth` is not supported for SSE or stdio connections.

## Prerequisites

- **Authentication is enabled.** The flow needs a signed-in `current_user`; an
  unauthenticated `useOAuth` request is rejected with `401`. User identifiers
  must be stable and must never be reassigned to another person: tokens are
  keyed by `user.identifier`, so reusing an identifier would hand one user
  another user's token.
- **`CHAINLIT_URL` is set** to the app's public URL. OAuth requires `https`
  outside loopback; `http://localhost` and `http://127.0.0.1` are allowed for a
  local development loop only. The callback URL is derived from this value, so
  the authorization code and state do not travel in clear text.
- **`CHAINLIT_COOKIE_SAMESITE` is compatible.** `lax` (the default) works for
  normal top-level pages. `none` is for cross-site contexts and always requires
  `Secure`. `strict` is not supported: the JWT cookie is not sent on the
  cross-site callback and the callback route answers `401`.
- **The app runs as a single process.** The token store, pending sign-ins and
  dynamic client registrations are in memory in one process. A callback that
  lands on another worker cannot see the flow or the token (L3).

## Configuration

OAuth needs two independent opt-ins plus the authorization-server allowlist.
For example:

```toml
[features.mcp]
enabled = true
oauth_authorization_origins = ["https://auth.example.com"]

[features.mcp.user_servers]
enabled = true
allowed_urls = ["https://mcp.example.com"]

[features.mcp.user_oauth]
enabled = true
max_starts_per_user_per_minute = 5
max_pending_per_user = 3
```

- `features.mcp.enabled` is the master MCP switch.
- `features.mcp.user_servers.enabled` must be on, and `allowed_urls` must
  include the MCP server. Entries match the scheme, hostname and effective port,
  plus an optional path subtree (`https://mcp.example.com` allows every path on
  that host; `https://mcp.example.com/team` allows only that subtree). An empty
  list denies every user-provided server. A single leading `*.` host label
  matches one or more subdomains but not the apex (`https://*.example.com`
  covers `a.example.com`, not `example.com` or `example.com.evil.com`); `*` in
  a path matches one or more characters within one segment and never crosses
  `/`, but the matched segment's sub-tree is still allowed as for a literal
  path (`https://mcp.example.com/tenant/*` covers
  `/tenant/alice/messages`). Wildcards in the scheme, port, credentials, query
  or fragment are rejected when the config loads.
- `features.mcp.oauth_authorization_origins` is the hard gate: when it is empty,
  every `useOAuth` request is rejected. List each authorization server as a bare
  origin (`https://auth.example.com`), with no path, query, fragment or
  userinfo. Invalid entries are rejected when the config loads. `http` is
  accepted only for loopback; use TLS for a remote authorization server.
- `features.mcp.user_oauth.enabled` is the second, independent opt-in. The
  `max_starts_per_user_per_minute` (default `5`) and
  `max_pending_per_user` (default `3`) limits bound sign-in attempts.
- The authorization server must support **Dynamic Client Registration**
  (RFC 7591). Chainlit registers a client per user and server and has no manual
  `client_id`/`client_secret` setting; an authorization server without DCR
  cannot complete the flow.

The MCP server's own origin is **not** an authorization-server entry. If the
authorization server shares an origin with the MCP server, list that origin
explicitly in `oauth_authorization_origins`.

## Connect a server

1. Open the MCP menu, choose to add a user-provided server, and select the
   **streamable-http** transport.
2. Enter a name and the MCP server URL, and tick **Sign in with OAuth
   (streamable HTTP only)**.
3. Connect. The server answers `202 authorization_required` and emits
   `mcp_authorization_required` with an authorization URL; the pending entry
   shows **Sign in to <host>**. Follow that link, approve access at the
   authorization server, and return to the chat; the connection completes when
   Chainlit receives `mcp_connected`.
4. You can remove a pending entry to cancel the flow.

With `@chainlit/react-client`, the same request is:

```ts
await api.connectUserMcp(
  sessionId,
  name,
  'streamable-http',
  url,
  undefined, // no headers with OAuth
  true // useOAuth
);
```

The MCP URL must be `http(s)`, must not contain userinfo, must be `https`
outside loopback, and its path must not contain `/.well-known/`. Any static
`headers` are rejected together with `useOAuth`: a shared credential and
per-user OAuth are mutually exclusive.

A sign-in must be completed within 5 minutes. Reloading the page abandons the
flow server-side; the entry can be removed and the connection retried.

## What is sent where

No access token, refresh token, authorization code or PKCE verifier is ever sent
to the browser, put in a socket payload, written to a response body or written
to disk. The authorization URL is the only OAuth value returned to the client:
it carries the single-use `state` and the PKCE challenge, both short-lived, and
is delivered only to the same user's browser through the `202` body and the
`mcp_authorization_required` socket event. The callback page is static.

The `state` is bound to the user who started the flow. A callback from another
user is rejected with `403` without consuming the pending flow; an unknown,
expired or replayed state is rejected with `400`.

## Token storage and lifetime

- Tokens and dynamic client registrations live in **memory**, keyed by
  `(user identifier, canonical MCP server URL)`. Nothing is persisted: a restart
  forces every user to authorize again, and DCR registrations are lost too.
- Access-token lifetime is controlled by the authorization server. The MCP SDK
  refreshes it transparently when the server issued a refresh token; otherwise
  the user has to sign in again when the access token expires.
- Disconnecting a server (`DELETE /mcp`) forgets that user's token and client
  registration for the server. A live tab keeps its already-loaded provider
  token until it disconnects.
- Logging out drops that user's tokens and cancels their pending flows, but does
  **not** revoke the grant at the authorization server (L6). A later sign-in with
  the same authorization server may reuse the existing consent.
- A registration that never produced a token expires after 1 hour. A pending
  sign-in expires after 5 minutes.
- The store is bounded: at most 20 entries per user and 500 per process, with
  least-recently-used eviction at flow start and on write. Evicting an entry
  forces that user to authorize again.

## Security boundaries and residuals

The OAuth request guard is a **request-role allowlist for the OAuth flow**, not
a network sandbox. It bounds the requests the flow makes on behalf of a
user-supplied MCP server:

- The marked MCP transport request may reach the server origin at any path.
- Every other request of the flow (protected-resource metadata,
  authorization-server metadata, dynamic client registration, token exchange
  and refresh) may reach only an operator-listed authorization-server origin,
  or the server origin for one of the two exact protected-resource metadata
  URLs on `GET`.

The server origin is therefore not an authorization-server entry: an
authorization server that lives on the MCP server's origin must be listed
explicitly.

The guard does **not** protect against:

- **Plain-path SSRF.** A non-OAuth user-provided MCP connection keeps its
  pre-existing URL surface; the guard only covers the OAuth flow.
- **DNS rebinding.** Origins are matched by name, not by resolved address; a
  listed hostname can resolve to a private address at request time (R13).
- **Custom `resource_metadata` URLs on the server origin.** A server that
  advertises its protected-resource metadata at a non-standard URL on its own
  origin is refused and the flow does not fall back (R10). Only the two exact
  discovery URLs are reachable.

Once `user_oauth.enabled` is on, any authenticated user can reach any listed
authorization server and can cause a client registration there. The allowlist
and the two opt-ins are the operator's controls; a listed authorization server
should be one you trust with your users' registrations.

With `CHAINLIT_COOKIE_SAMESITE=none`, a cross-site `POST /logout` can sign the
user out and drop that user's MCP tokens, because the logout route has no CSRF
or `Origin` check. This is an accepted residual (L9); `lax` is the default and
blocks it. The impact is a forced sign-out and credential re-entry, not token
theft.

## Troubleshooting

| Detail                                                                                           | Fix                                                                        |
| ------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| `This app does not support MCP.`                                                                 | Set `features.mcp.enabled = true`.                                         |
| `MCP OAuth is disabled. Set features.mcp.user_oauth.enabled = true.`                             | Enable the second opt-in.                                                  |
| `MCP OAuth requires an authorization server origin in features.mcp.oauth_authorization_origins.` | Add the authorization server's bare origin to the allowlist.               |
| `OAuth requires an https CHAINLIT_URL (http is only allowed for localhost).`                     | Set `CHAINLIT_URL` to the public `https` URL.                              |
| `OAuth cannot be combined with custom headers.`                                                  | Remove the static `headers`; use OAuth instead.                            |
| `OAuth requires an http(s) MCP URL without userinfo and https outside localhost.`                | Fix the MCP URL.                                                           |
| `OAuth MCP URLs cannot contain /.well-known/.`                                                   | Use the MCP endpoint path, not a discovery path.                           |
| `OAuth is not supported for SSE MCP connections; use streamable HTTP.`                           | Switch the transport.                                                      |
| `Too many MCP authorization attempts; wait a minute and retry.`                                  | Wait for the rate-limit window, or raise `max_starts_per_user_per_minute`. |
| `Too many MCP authorizations are in progress; finish one first.`                                 | Finish or cancel a pending flow, or raise `max_pending_per_user`.          |
| `The MCP authorization was not completed in time.`                                               | Retry; the sign-in took longer than 5 minutes.                             |

The design record for this feature, including its threat model, is
[Per-user OAuth for HTTP MCP connections](research/mcp-per-user-oauth.md).
