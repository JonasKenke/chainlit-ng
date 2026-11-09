import os
from typing import Dict, List, Optional

import chainlit as cl
import chainlit.data as cl_data
from chainlit.element import Element, ElementDict
from chainlit.step import StepDict
from chainlit.types import (
    Feedback,
    PageInfo,
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
)
from chainlit.utils import utc_now

os.environ["CHAINLIT_AUTH_SECRET"] = "SUPER_SECRET"  # nosec B105

now = utc_now()
THREADS: Dict[str, ThreadDict] = {}


class MemoryDataLayer(cl_data.BaseDataLayer):
    async def get_user(self, identifier: str):
        return cl.PersistedUser(id=identifier, createdAt=now, identifier=identifier)

    async def create_user(self, user: cl.User):
        return cl.PersistedUser(
            id=user.identifier, createdAt=now, identifier=user.identifier
        )

    async def delete_feedback(self, feedback_id: str) -> bool:
        return True

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return ""

    async def create_element(self, element: "Element"):
        pass

    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional["ElementDict"]:
        return None

    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        pass

    async def create_step(self, step_dict: "StepDict"):
        thread = THREADS.get(step_dict.get("threadId", ""))
        if thread is None:
            thread = {
                "id": step_dict.get("threadId", ""),
                "createdAt": now,
                "userIdentifier": "question-card-user",
                "steps": [],
            }
            THREADS[thread["id"]] = thread
        self._upsert_step(thread, step_dict)

    async def update_step(self, step_dict: "StepDict"):
        thread_id = step_dict.get("threadId")
        thread = THREADS.get(thread_id or "")
        if thread is None:
            await self.create_step(step_dict)
            return
        self._upsert_step(thread, step_dict)

    @staticmethod
    def _upsert_step(thread: ThreadDict, step_dict: "StepDict"):
        steps = thread.setdefault("steps", [])
        for index, step in enumerate(steps):
            if step.get("id") == step_dict.get("id"):
                steps[index] = step_dict
                return
        steps.append(step_dict)

    async def delete_step(self, step_id: str):
        pass

    async def get_thread_author(self, thread_id: str) -> str:
        thread = THREADS.get(thread_id)
        return thread["userIdentifier"] if thread else ""

    async def delete_thread(self, thread_id: str):
        THREADS.pop(thread_id, None)

    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        thread = THREADS.setdefault(
            thread_id,
            {
                "id": thread_id,
                "createdAt": now,
                "userIdentifier": user_id or "question-card-user",
                "steps": [],
            },
        )
        if name:
            thread["name"] = name
        if user_id:
            thread["userId"] = user_id
            thread["userIdentifier"] = user_id
        if metadata is not None:
            thread["metadata"] = metadata
        if tags is not None:
            thread["tags"] = tags

    async def list_threads(
        self, pagination: Pagination, filters: ThreadFilter
    ) -> PaginatedResponse[ThreadDict]:
        threads = list(THREADS.values())
        if filters.userId:
            threads = [t for t in threads if t.get("userId") == filters.userId]
        return PaginatedResponse(
            data=threads,
            pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
        )

    async def get_thread(self, thread_id: str) -> "Optional[ThreadDict]":
        thread = THREADS.get(thread_id)
        if thread:
            thread["steps"] = sorted(
                thread.get("steps", []), key=lambda step: step.get("createdAt", "")
            )
        return thread

    async def get_favorite_steps(self, user_id: str) -> List["StepDict"]:
        return []

    async def close(self) -> None:
        pass


@cl.data_layer
def data_layer():
    return MemoryDataLayer()


@cl.header_auth_callback
async def header_auth(headers):
    if headers.get("test-header"):
        return cl.PersistedUser(
            id="question-card-user", createdAt=now, identifier="question-card-user"
        )
    return None


@cl.on_message
async def on_message(message: cl.Message):
    if message.content == "ask-text":
        prompt = "What should I name the release?"
        options: List[cl.QuestionOption] = []
        allow_free_text = True
    elif message.content == "ask-option":
        prompt = "Which environment should I deploy to?"
        options = [
            cl.QuestionOption(
                id="staging", label="Staging", description="Preview environment"
            ),
            cl.QuestionOption(id="production", label="Production"),
        ]
        allow_free_text = False
    else:
        await cl.Message(content="ready").send()
        return

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
        # Never echo the typed answer: this app exists to prove that a shared
        # thread carries no free text outside the redacted question record.
        result = "answered privately"
    await cl.Message(content=result).send()
