import os

import chainlit as cl

os.environ["CHAINLIT_AUTH_SECRET"] = "SUPER_SECRET"  # nosec B105


@cl.header_auth_callback
async def header_auth(headers):
    if headers.get("test-header"):
        return cl.User(identifier="question-user")
    return None


async def ask_and_report(prompt, options, allow_free_text):
    # The step binds the ask; with cot="hidden" it must stay out of the UI.
    async with cl.Step(name="question-step", id="question-step", type="tool") as step:
        answer = await cl.ask(
            prompt,
            options=options,
            allow_free_text=allow_free_text,
            timeout=120,
            step=step,
        )

    if answer.option_id:
        result = f"answered option={answer.option_id} label={answer.label}"
    else:
        result = f"answered text={answer.text}"
    await cl.Message(content=result).send()


@cl.on_message
async def on_message(message: cl.Message):
    if message.content == "option":
        await ask_and_report(
            "Which environment should I deploy to?",
            [
                cl.QuestionOption(
                    id="staging",
                    label="Staging",
                    description="Preview environment",
                ),
                cl.QuestionOption(id="production", label="Production"),
            ],
            False,
        )
    elif message.content == "text":
        await ask_and_report("What should I name the release?", [], True)
