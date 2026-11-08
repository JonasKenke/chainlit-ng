# Request context in callbacks

`cl.request()` returns the `starlette.requests.Request` for the current
connection. It returns `None` when the app runs outside a web request, for
example in an unrelated background task.

## Reading headers, cookies, query and path

```python
import chainlit as cl


@cl.on_chat_start
async def start():
    request = cl.request()
    if request is None:
        return

    locale = request.headers.get("accept-language")
    page = request.query_params.get("page")
    cl.user_session.set("locale", locale)
    cl.user_session.set("page", page)
```

The request is built from the server-side ASGI scope. In websocket callbacks it
is the handshake that opened the connection, normalized to an HTTP `GET` view.
In plain HTTP routes it is the request being served. Reading the body or form is
not supported.

`cl.context.session.request` returns the same connection request from inside a
Chainlit context.

## Optional `request` parameter

Callbacks that need the request can declare an optional `request` parameter.
Chainlit detects it by signature, so callbacks that do not declare it behave
exactly as before.

```python
from starlette.requests import Request

import chainlit as cl
from chainlit.types import ThreadDict


@cl.on_chat_start
async def start(request: Request):
    tenant = request.headers.get("x-tenant")


@cl.on_chat_resume
async def resume(thread: ThreadDict, request: Request):
    token = request.cookies.get("session")


@cl.on_message
async def message(msg: cl.Message, request: Request):
    source = request.headers.get("referer")
```

The parameter is supported on `on_chat_start`, `on_chat_resume` and
`on_message`. It may be `None` for non-web clients, such as scheduled tasks or a
chat platform integration without an HTTP request.

## Trust boundary

Headers and cookies come only from the server-side request. Values in a chat
message, element, action or any other client-supplied payload are not identity
and must not be treated as authorization. `header_auth_callback` remains the
authorization decision; the request is read afterwards for extra context. Do not
log headers or cookies — they can contain credentials and session tokens.
