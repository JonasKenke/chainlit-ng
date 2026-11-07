import chainlit as cl


@cl.on_chat_start
async def on_start():
    await cl.Message(
        content="Custom elements with loader errors",
        elements=[
            cl.CustomElement(name="MissingHelper", props={}),
            cl.CustomElement(name="Circular", props={}),
        ],
    ).send()
