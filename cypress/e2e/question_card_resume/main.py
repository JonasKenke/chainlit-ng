"""Question-card app with thread resume enabled for the resumable reload spec."""

import runpy
from pathlib import Path

import chainlit as cl

runpy.run_path(
    str(Path(__file__).resolve().parent.parent / "question_card" / "main.py")
)


@cl.on_chat_resume
async def on_chat_resume(thread):
    pass
