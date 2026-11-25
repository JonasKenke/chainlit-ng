"""Strong references for fire-and-forget persistence tasks.

The event loop only keeps a weak reference to a task, so a task whose only
reference was the ``asyncio.create_task`` call can be garbage collected while it
is suspended at an ``await``. Persistence writes are fire-and-forget; keep a
strong reference to each one until it completes.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from chainlit.logger import logger

T = TypeVar("T")

_persistence_tasks: set[asyncio.Task[Any]] = set()


def _retrieve_result(task: asyncio.Task[Any]) -> None:
    _persistence_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Persistence task failed: %s",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


def create_persistence_task(coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
    task = asyncio.create_task(coro)
    _persistence_tasks.add(task)
    task.add_done_callback(_retrieve_result)
    return task
