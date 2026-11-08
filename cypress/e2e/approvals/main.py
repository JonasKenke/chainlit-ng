import os

from fastapi.responses import JSONResponse
from starlette.requests import Request

import chainlit as cl
from chainlit.server import app, sio
from chainlit.socket import connection_successful as chainlit_connection_successful

os.environ["CHAINLIT_AUTH_SECRET"] = "SUPER_SECRET"  # nosec B105

connections = 0


@cl.header_auth_callback
async def header_auth(headers):
    if headers.get("test-header"):
        return cl.User(identifier="approvals-user")
    return None


@sio.on("connection_successful")
async def count_connections(sid):
    global connections
    connections += 1
    # Registering a handler replaces Chainlit's, so delegate the real work.
    return await chainlit_connection_successful(sid)


@app.middleware("http")
async def test_endpoints(request: Request, call_next):
    # Middleware runs before the SPA catch-all, unlike routes added after it.
    if request.url.path == "/test/connections":
        return JSONResponse({"count": connections})
    return await call_next(request)


@cl.on_message
async def on_message(message: cl.Message):
    if message.content != "ask":
        await cl.Message(content="ready").send()
        return

    async with cl.Step(name="send_email", type="tool") as step:
        decision = await cl.Approval(
            step=step, arguments={"to": "person@example.com"}, timeout=120
        ).ask()

    if decision.approved:
        await cl.Message(content="executed").send()
    else:
        await cl.Message(content=f"not executed: {decision.decision}").send()
