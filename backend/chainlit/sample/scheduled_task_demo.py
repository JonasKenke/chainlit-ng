"""Minimal scheduled prompt example.

Enable ``[features].prompt_scheduling`` and configure a data layer to use it.
"""

import chainlit as cl

MODEL_MAPPING = {
    "⚡ GPT 5 Mini": "gpt-5-mini",
    "⚖️ GPT 5": "gpt-5",
    "🔷 Gemini 3.5 Flash": "gemini-3.5-flash",
    "🔹 Gemini 3.1 Flash Lite": "gemini-3.1-flash-lite",
}


@cl.set_scheduled_task_models
async def scheduled_task_models(_user, _language):
    return list(MODEL_MAPPING)


@cl.on_chat_start
async def start_chat():
    default_model = "🔷 Gemini 3.5 Flash"
    cl.user_session.set("selected_model", default_model)
    await cl.ChatSettings(
        [
            cl.input_widget.Tab(
                id="general",
                label="General",
                inputs=[
                    cl.input_widget.Select(
                        id="model",
                        label="Model",
                        values=list(MODEL_MAPPING),
                        initial_value=default_model,
                    )
                ],
            )
        ]
    ).send()


@cl.on_settings_update
async def update_settings(settings):
    model = settings.get("model")
    if model in MODEL_MAPPING:
        cl.user_session.set("selected_model", model)


@cl.on_message
async def on_message(message: cl.Message):
    if task_id := (message.metadata or {}).get("scheduledTaskId"):
        print(
            f"Scheduled run started: {task_id} "
            f"(model={cl.user_session.get('selected_model') or 'default'})",
            flush=True,
        )
    await cl.Message(content=f"Echo: {message.content}").send()
