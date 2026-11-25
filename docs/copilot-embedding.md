# Embedding Chainlit NG Copilot: supported authentication model

Copilot is a shadow-DOM widget mounted into the host page. Its REST and
WebSocket requests go to the Chainlit NG origin, and both authenticate with the
`access_token` cookie (set by `/login`, `/auth/jwt`, or the OAuth callback).

The browser decides whether that cookie is first-party or third-party from the
relationship between the host page and the Chainlit NG origin. That relationship,
not the widget configuration, decides whether Copilot can authenticate.

## Support matrix

| Host page vs. Chainlit NG origin                                                      | Cookie behavior                                                                         | Supported                                                                              |
| ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Same origin, or same registrable domain (e.g. `www.example.com` + `chat.example.com`) | First-party/same-site cookie; the default `SameSite=lax` cookie is sent.                | **Yes** (recommended)                                                                  |
| Different registrable domain, `CHAINLIT_COOKIE_SAMESITE=none`, HTTPS                  | Third-party cookie; works only where the browser allows third-party cookies.            | **Conditional.** Not Safari/ITP and not private browsing.                              |
| Different registrable domain, cookie blocked                                          | `/auth/jwt` returns 200 but stores no session cookie; the socket connection is refused. | **No.** Serve Chainlit NG on the same site, or put a same-origin proxy in front of it. |

"Same-site" means the registrable domain (eTLD+1) and scheme: `customer.com` +
`chat.customer.com` is same-site, but `customer.com` + `chainlit.app` is not.

## HTTPS and `CHAINLIT_COOKIE_SAMESITE`

`CHAINLIT_COOKIE_SAMESITE` accepts `lax` (default), `strict`, or `none`.
Cross-site cookie transport requires `none`; Chainlit NG then also marks the cookie
`Secure`, so the Chainlit NG origin must be served over HTTPS. Browsers usually
exempt `localhost` during development.

`SameSite=none` enables cookie transport. It does not make the cookie survive
third-party cookie blocking.

## Safari / ITP and private browsing

Safari's Intelligent Tracking Prevention (ITP) and private browsing drop
third-party cookies for this flow. `/auth/jwt` still returns 200, so the widget
cannot tell from the response that the `Set-Cookie` was discarded; the
WebSocket connect is then refused.

Do not claim cross-site Copilot support on Safari. Partitioned cookies (CHIPS)
and the Storage Access API are not dependable here: Safari disabled partitioned
cookies in current versions, and the Storage Access API needs a user gesture.
The upstream issue,
[chainlit#2013](https://github.com/Chainlit/chainlit/issues/2013), remains open.

## Recommended deployment: same site or proxy

1. **Serve Chainlit NG on the same registrable domain as the host page**, for
   example `chat.example.com` for a widget on `www.example.com`. The cookie is
   same-site, so the default `SameSite=lax` works and Safari is fine.
2. **Proxy Chainlit NG through the host origin** when a subdomain is not possible.
   Route a path on the host site (e.g. `https://www.example.com/chainlit`) to the
   Chainlit NG server, so the browser sees one origin. The proxy must forward HTTP
   and the WebSocket upgrade for `/ws/socket.io`, and preserve cookies and
   redirects. Set `CHAINLIT_ROOT_PATH` when Chainlit NG is mounted under a path.

Neither option requires an application code change.

## Blocked-cookie diagnostic

After `/auth/jwt` succeeds, Copilot verifies the session with `GET /user`. When
that check returns 401, the widget shows:

> Authentication failed. This browser may be blocking third-party cookies.
> Serve Chainlit NG on the same site as this page (recommended), or allow
> third-party cookies for this site.

A transient network failure keeps the existing error path and does not show the
cookie message. The diagnostic does not change cookie settings, server auth, or
the public API.

## Configuration reference

- `CHAINLIT_COOKIE_SAMESITE` — `lax` (default), `strict`, or `none`; `none`
  implies `Secure`.
- `CHAINLIT_AUTH_COOKIE_NAME` — auth cookie name (default `access_token`).
- `CHAINLIT_AUTH_COOKIE_PATH` / `CHAINLIT_ROOT_PATH` — cookie path for a
  sub-path or proxied deployment.
- `[project] allow_origins` in `.chainlit/config.toml` — the host origin must be
  listed explicitly for a cookie-authenticated cross-origin widget. A wildcard
  (`["*"]`) no longer grants credentialed CORS: it serves `Access-Control-Allow-
Origin: *` without credentials, and Engine.IO only accepts same-origin
  WebSockets when login is enabled. `[project] allow_credentials = true` keeps
  the legacy wildcard-credential behavior for existing deployments but reflects
  any caller's Origin and is not recommended.
- Window messages (`on_window_message` / `cl.send_window_message`) are accepted
  only from the app origin or an explicit `allow_origins` entry; a wildcard is
  never treated as a trusted parent.

## What is not supported

- Cross-site Copilot on Safari/ITP or in private browsing.
- Cookie-free token transport for the WebSocket, REST API, and element
  subresources. Cross-site media (images, PDF, audio, video, files, dataframes,
  Plotly) stays cookie-authenticated and breaks when the cookie is blocked.
