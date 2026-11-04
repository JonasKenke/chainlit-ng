import os
from uuid import uuid4

from fastapi import Request, Response

import chainlit as cl
from chainlit.auth import create_jwt
from chainlit.server import _authenticate_user, router
from chainlit.user import User

os.environ["CHAINLIT_AUTH_SECRET"] = "SUPER_SECRET"  # nosec B105
os.environ["CHAINLIT_CUSTOM_AUTH"] = "true"


@router.get("/auth/custom")
async def custom_auth(request: Request) -> Response:
    user_id = str(uuid4())

    user = User(identifier=user_id, metadata={"role": "user"})
    response = await _authenticate_user(request, user)

    return response


@router.get("/auth/token")
async def custom_token_auth() -> Response:
    user_id = str(uuid4())

    user = User(identifier=user_id, metadata={"role": "admin"})
    response = create_jwt(user)

    return response


catch_all_route = next(
    (
        route
        for route in router.routes
        if getattr(route, "path", None) == "/{full_path:path}"
    ),
    None,
)

if catch_all_route:
    router.routes.remove(catch_all_route)
    router.routes.append(catch_all_route)
    # Invalidate FastAPI's cached candidates after reordering the router.
    if hasattr(router, "_mark_routes_changed"):
        router._mark_routes_changed()


@cl.on_chat_start
async def on_chat_start():
    user = cl.user_session.get("user")
    await cl.Message(f"Hello {user.identifier}").send()


@cl.on_message
async def on_message(msg: cl.Message):
    await cl.Message(content=f"Echo: {msg.content}").send()
