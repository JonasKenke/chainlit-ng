import os

import chainlit as cl
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


@app.get("/test/connections")
async def get_connections():
    return {"count": connections}


@app.get("/test/disconnect")
async def disconnect_current_socket():
    if last_socket_id:
        await sio.disconnect(last_socket_id)
    return {"ok": True}


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
