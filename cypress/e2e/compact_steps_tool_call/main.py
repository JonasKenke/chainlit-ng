import chainlit as cl


@cl.step(type="tool", name="tool1")
async def tool1():
    return "Result from tool1"


@cl.step(type="tool", name="tool2")
async def tool2():
    return "Result from tool2"


@cl.step(type="undefined", name="wrapper")
async def wrapper():
    # Non-tool steps do not count in tool_call mode and are skipped by the
    # existing renderer, but their children are still displayed.
    await tool1()
    await tool2()
    return "Result from wrapper"


@cl.on_message
async def main(message: cl.Message):
    await wrapper()
    await cl.Message(content="Final answer").send()
