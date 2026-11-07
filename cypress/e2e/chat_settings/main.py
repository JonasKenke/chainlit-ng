import chainlit as cl
from chainlit.input_widget import Select, Slider, Switch


@cl.on_chat_start
async def start():
    await cl.ChatSettings(
        [
            Select(
                id="Model",
                label="OpenAI - Model",
                values=["gpt-3.5-turbo", "gpt-3.5-turbo-16k", "gpt-4", "gpt-4-32k"],
                initial_index=0,
            ),
            Switch(id="Streaming", label="OpenAI - Stream Tokens", initial=True),
            Slider(
                id="Temperature",
                label="OpenAI - Temperature",
                initial=1,
                min=0,
                max=2,
                step=0.1,
            ),
            Slider(
                id="SAI_Steps",
                label="Stability AI - Steps",
                initial=30,
                min=0,
                max=150,
                step=1,
                description="Amount of inference steps performed on image generation.",
            ),
            Slider(
                id="SAI_Cfg_Scale",
                label="Stability AI - Cfg_Scale",
                initial=7,
                min=1,
                max=35,
                step=0.1,
                description="Influences how strongly your generation is guided to match your prompt.",
            ),
            Slider(
                id="SAI_Width",
                label="Stability AI - Image Width",
                initial=512,
                min=0,
                max=2048,
                step=64,
                tooltip="Measured in pixels",
            ),
            Slider(
                id="SAI_Height",
                label="Stability AI - Image Height",
                initial=512,
                min=0,
                max=2048,
                step=64,
                tooltip="Measured in pixels",
            ),
            Select(
                id="country",
                label="Country",
                items={"United States": "US", "Canada": "CA"},
                initial_value="US",
            ),
            Select(
                id="city",
                label="City",
                items={"Other": "other"},
                depends_on="country",
                dependent_items={
                    "US": {"New York": "ny", "San Francisco": "sf"},
                    "CA": {"Toronto": "toronto"},
                },
                initial_value="ny",
            ),
        ]
    ).send()


@cl.on_settings_update
async def setup_agent(settings):
    await cl.Message(
        content=f"Settings updated! country={settings.get('country')} city={settings.get('city')}"
    ).send()
