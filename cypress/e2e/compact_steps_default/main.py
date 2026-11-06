import chainlit as cl


@cl.step(type="tool", name="tool1")
async def tool1():
    return "Result from tool1"


@cl.step(type="tool", name="tool2")
async def tool2():
    return "Result from tool2"


@cl.step(type="tool", name="tool3")
async def tool3():
    return "Result from tool3"


@cl.on_message
async def main(message: cl.Message):
    await tool1()
    await tool2()
    await tool3()
    await cl.Message(content="Final answer").send()
