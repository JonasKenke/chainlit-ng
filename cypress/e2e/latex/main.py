import chainlit as cl


@cl.on_message
async def main(message: cl.Message):
    content = message.content.strip().lower()

    if content == "stream":
        # Split the delimiters across tokens and pause before closing them so
        # the spec can see the unclosed delimiter as plain text.
        msg = cl.Message(content="")
        for token in ["Solving ", "\\(", " x", " + 1"]:
            await msg.stream_token(token)
        await cl.sleep(2)
        await msg.stream_token(" \\)")
        await msg.send()
        return

    await cl.Message(
        content=(
            "Ohm: \\( R = V / I \\) done\n\n"
            "\\[ y = ax^2 + bx + c \\]\n\n"
            "Dollar math: $e^{i\\pi}+1=0$ and\n\n"
            "$$\nz = x + y\n$$\n\n"
            "Inline code `\\(x\\)` and fenced code:\n\n"
            "```\n\\[y\\]\n```"
        )
    ).send()
