from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Dict, List, Optional

from chainlit.types import (
    Feedback,
    PaginatedResponse,
    Pagination,
    PromptDict,
    ScheduledTaskCompletion,
    ScheduledTaskDict,
    ThreadDict,
    ThreadFilter,
)

from .utils import queue_until_user_message

if TYPE_CHECKING:
    from chainlit.element import Element, ElementDict
    from chainlit.step import StepDict
    from chainlit.user import PersistedUser, User


class BaseDataLayer(ABC):
    """Base class for data persistence."""

    @abstractmethod
    async def get_user(self, identifier: str) -> Optional["PersistedUser"]:
        pass

    @abstractmethod
    async def create_user(self, user: "User") -> Optional["PersistedUser"]:
        pass

    @abstractmethod
    async def delete_feedback(
        self,
        feedback_id: str,
    ) -> bool:
        pass

    @abstractmethod
    async def upsert_feedback(
        self,
        feedback: Feedback,
    ) -> str:
        pass

    @queue_until_user_message()
    @abstractmethod
    async def create_element(self, element: "Element"):
        pass

    @abstractmethod
    async def get_element(
        self, thread_id: str, element_id: str
    ) -> Optional["ElementDict"]:
        pass

    @queue_until_user_message()
    @abstractmethod
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        pass

    @queue_until_user_message()
    @abstractmethod
    async def create_step(self, step_dict: "StepDict"):
        pass

    @queue_until_user_message()
    @abstractmethod
    async def update_step(self, step_dict: "StepDict"):
        pass

    @queue_until_user_message()
    @abstractmethod
    async def delete_step(self, step_id: str):
        pass

    @abstractmethod
    async def get_thread_author(self, thread_id: str) -> str:
        return ""

    @abstractmethod
    async def delete_thread(self, thread_id: str):
        pass

    async def delete_all_threads(self, user_id: str) -> int:
        """Delete all threads owned by ``user_id``.

        Implementations must scope the deletion strictly to the persisted
        ``userId``, delete storage objects owned by the deleted threads, and
        return the number of deleted threads. The operation must be
        idempotent: a second call returns 0.
        """
        raise NotImplementedError(
            "Bulk thread deletion is not implemented for this data layer"
        )

    @abstractmethod
    async def list_threads(
        self, pagination: "Pagination", filters: "ThreadFilter"
    ) -> "PaginatedResponse[ThreadDict]":
        pass

    @abstractmethod
    async def get_thread(self, thread_id: str) -> "Optional[ThreadDict]":
        pass

    @abstractmethod
    async def update_thread(
        self,
        thread_id: str,
        name: Optional[str] = None,
        user_id: Optional[str] = None,
        metadata: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
    ):
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    async def get_favorite_steps(self, user_id: str) -> List["StepDict"]:
        pass

    async def set_step_favorite(
        self, step_dict: "StepDict", favorite: bool
    ) -> "StepDict":
        metadata = step_dict.get("metadata") or {}
        metadata["favorite"] = favorite
        step_dict["metadata"] = metadata
        await self.update_step(step_dict)
        return step_dict

    # ---- Prompt Gallery (opt-in; non-abstract so existing data layers stay valid) ----

    async def create_prompt(self, prompt: PromptDict) -> PromptDict:
        raise NotImplementedError(
            "Prompt gallery is not implemented for this data layer"
        )

    async def list_prompts(self, user_id: str) -> List[PromptDict]:
        raise NotImplementedError(
            "Prompt gallery is not implemented for this data layer"
        )

    async def get_prompt(self, prompt_id: str) -> Optional[PromptDict]:
        raise NotImplementedError(
            "Prompt gallery is not implemented for this data layer"
        )

    async def update_prompt(self, prompt: PromptDict) -> PromptDict:
        raise NotImplementedError(
            "Prompt gallery is not implemented for this data layer"
        )

    async def delete_prompt(self, prompt_id: str, user_id: str) -> bool:
        raise NotImplementedError(
            "Prompt gallery is not implemented for this data layer"
        )

    # ---- Per-user skills (opt-in; share the prompts table in SQLAlchemy) ----

    async def create_skill(self, skill: PromptDict) -> PromptDict:
        raise NotImplementedError("Skills are not implemented for this data layer")

    async def list_skills(self, user_id: str) -> List[PromptDict]:
        raise NotImplementedError("Skills are not implemented for this data layer")

    async def get_skill(self, skill_id: str, user_id: str) -> Optional[PromptDict]:
        raise NotImplementedError("Skills are not implemented for this data layer")

    async def update_skill(self, skill: PromptDict) -> PromptDict:
        raise NotImplementedError("Skills are not implemented for this data layer")

    async def delete_skill(self, skill_id: str, user_id: str) -> bool:
        raise NotImplementedError("Skills are not implemented for this data layer")

    # ---- Prompt scheduling (opt-in; non-abstract for compatibility) ----

    async def create_scheduled_task(self, task: ScheduledTaskDict) -> ScheduledTaskDict:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def list_scheduled_tasks(self, user_id: str) -> List[ScheduledTaskDict]:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def get_scheduled_task(self, task_id: str) -> Optional[ScheduledTaskDict]:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def update_scheduled_task(self, task: ScheduledTaskDict) -> ScheduledTaskDict:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def delete_scheduled_task(self, task_id: str, user_id: str) -> bool:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def request_scheduled_task_run(
        self, task_id: str, user_id: str, requested_at: str
    ) -> bool:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def claim_due_scheduled_tasks(
        self, now: str, lease_until: str, worker_id: str, limit: int
    ) -> List[ScheduledTaskDict]:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def renew_scheduled_task_lease(
        self, task_id: str, lease_id: str, lease_until: str
    ) -> bool:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )

    async def complete_scheduled_task_run(
        self, task_id: str, lease_id: str, completion: ScheduledTaskCompletion
    ) -> bool:
        raise NotImplementedError(
            "Prompt scheduling is not implemented for this data layer"
        )
