import os

import chainlit as cl
from fastapi.responses import JSONResponse
from starlette.requests import Request

from chainlit.server import app, sio

os.environ["CHAINLIT_AUTH_SECRET"] = "SUPER_SECRET"  # nosec B105

connections = 0
last_socket_id = None


@cl.header_auth_callback
async def header_auth(headers):
    if headers.get("test-header"):
        return cl.User(identifier="approvals-user")
    return None


@sio.on("connection_successful")
async def count_connections(sid):
    global connections
    connections += 1


@app.middleware("http")
async def test_endpoints(request: Request, call_next):
    # Middleware runs before the SPA catch-all, unlike routes added after it.
    if request.url.path == "/test/connections":
        return JSONResponse({"count": connections})
    if request.url.path == "/test/disconnect":
        if last_socket_id:
            await sio.disconnect(last_socket_id)
        return JSONResponse({"ok": True})
    return await call_next(request)


@cl.on_chat_start
async def on_chat_start():
    global last_socket_id
    last_socket_id = cl.context.session.socket_id


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
