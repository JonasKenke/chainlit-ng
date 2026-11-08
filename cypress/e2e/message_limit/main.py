import chainlit as cl


@cl.on_chat_start
async def start():
    await cl.Message(content="Ready").send()


@cl.on_message
async def main(message: cl.Message):
    await cl.Message(content=f"Echo length: {len(message.content)}").send()
