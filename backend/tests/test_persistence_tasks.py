"""Persistence tasks must stay alive until the write completes.

The event loop only holds a weak reference to a task, so a fire-and-forget
persistence task can be garbage collected while it is suspended at an ``await``.
These tests block a write, drop every strong reference held by the test, force a
collection, and assert the write still lands. Before the fix the task and the
future it awaits form an unreachable cycle, so ``gc.collect()`` silently kills
the write.
"""

import asyncio
import gc
import uuid
import weakref
from pathlib import Path
from unittest.mock import AsyncMock, patch

from chainlit import task_refs
from chainlit.context import init_http_context
from chainlit.element import Text
from chainlit.emitter import ChainlitEmitter
from chainlit.message import Message
from chainlit.step import Step
from chainlit.user import PersistedUser


class GatedWrite:
    """A persistence call that blocks until the test opens its gate."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.future_ref: weakref.ReferenceType[asyncio.Future] | None = None
        self.completed = False

    async def __call__(self, *args, **kwargs):
        # Only a weak reference to the future leaves this frame. The task, its
        # coroutine frame and this future therefore form a cycle that the
        # garbage collector can collect unless the task registry holds it.
        future = asyncio.get_running_loop().create_future()
        self.future_ref = weakref.ref(future)
        self.entered.set()
        await future
        self.completed = True

    def release(self):
        future = self.future_ref()
        assert future is not None, "the paused write was garbage collected"
        future.set_result(None)


def _new_tasks(before: set[asyncio.Task]) -> set[asyncio.Task]:
    return {task for task in asyncio.all_tasks() if task not in before}


async def _assert_registry_clean(baseline):
    for _ in range(10):
        leaked = set(task_refs._persistence_tasks) - baseline
        if not leaked:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"completed persistence tasks were left in the registry: {leaked}"
    )


async def _assert_survives_gc(trigger, gates):
    """Run ``trigger``, collect garbage while its writes are in flight.

    The spawned tasks must still be alive after the collection, then every write
    must complete and leave the registry.
    """
    registry_before = set(task_refs._persistence_tasks)
    tasks_before = asyncio.all_tasks()

    await trigger()
    for gate in gates:
        await asyncio.wait_for(gate.entered.wait(), 1)

    spawned = _new_tasks(tasks_before)
    assert len(spawned) == len(gates), f"expected {len(gates)} tasks, got {spawned}"

    refs = [weakref.ref(task) for task in spawned]
    del spawned
    gc.collect()

    alive = [ref() for ref in refs]
    assert all(task is not None for task in alive), (
        "a persistence task was garbage collected while it was writing"
    )

    for gate in gates:
        gate.release()

    for task in alive:
        assert task is not None
        await asyncio.wait_for(task, 1)

    assert all(gate.completed for gate in gates), "the data-layer write never ran"
    await _assert_registry_clean(registry_before)


async def test_create_persistence_task_holds_until_done():
    registry_before = set(task_refs._persistence_tasks)
    gate = GatedWrite()

    task = task_refs.create_persistence_task(gate())
    await asyncio.wait_for(gate.entered.wait(), 1)

    task_ref = weakref.ref(task)
    del task
    gc.collect()

    assert task_ref() is not None, "the registry did not keep the task alive"

    gate.release()
    held_task = task_ref()
    assert held_task is not None
    await asyncio.wait_for(held_task, 1)

    assert gate.completed
    await _assert_registry_clean(registry_before)


async def test_message_writes_survive_gc(mock_chainlit_context):
    async with mock_chainlit_context:
        data_layer = AsyncMock()
        create_gate = GatedWrite()
        update_gate = GatedWrite()
        delete_gate = GatedWrite()
        data_layer.create_step = create_gate
        data_layer.update_step = update_gate
        data_layer.delete_step = delete_gate
        data_layer.get_thread = AsyncMock(return_value=None)

        message = Message(content="hello", id="msg_123")

        with (
            patch("chainlit.message.get_data_layer", return_value=data_layer),
            patch("chainlit.message.chat_context"),
        ):
            await _assert_survives_gc(message.update, [update_gate])
            await _assert_survives_gc(message.remove, [delete_gate])
            await _assert_survives_gc(message._create, [create_gate])


async def test_step_writes_survive_gc(mock_chainlit_context):
    async with mock_chainlit_context:
        data_layer = AsyncMock()
        create_gate = GatedWrite()
        update_gate = GatedWrite()
        delete_gate = GatedWrite()
        data_layer.create_step = create_gate
        data_layer.update_step = update_gate
        data_layer.delete_step = delete_gate

        with patch("chainlit.step.get_data_layer", return_value=data_layer):
            await _assert_survives_gc(Step(name="step").send, [create_gate])
            await _assert_survives_gc(Step(name="step").update, [update_gate])
            await _assert_survives_gc(Step(name="step").remove, [delete_gate])


async def test_step_context_manager_writes_survive_gc(mock_chainlit_context):
    async with mock_chainlit_context:
        data_layer = AsyncMock()
        create_gate = GatedWrite()
        update_gate = GatedWrite()
        data_layer.create_step = create_gate
        data_layer.update_step = update_gate

        async def trigger():
            with Step(name="step"):
                pass

        with patch("chainlit.step.get_data_layer", return_value=data_layer):
            await _assert_survives_gc(trigger, [create_gate, update_gate])


async def test_element_write_survives_gc(mock_chainlit_context):
    async with mock_chainlit_context:
        create_gate = GatedWrite()
        data_layer = AsyncMock()
        data_layer.create_element = create_gate
        element = Text(name="doc.txt", content="body", url="http://x/doc.txt")

        with patch("chainlit.element.get_data_layer", return_value=data_layer):
            await _assert_survives_gc(element._create, [create_gate])


async def test_init_http_context_write_survives_gc():
    update_gate = GatedWrite()
    data_layer = AsyncMock()
    data_layer.update_thread = update_gate
    user = PersistedUser(
        id="user_123", createdAt="2024-01-01T00:00:00Z", identifier="tester"
    )

    async def trigger():
        with (
            patch("chainlit.data.get_data_layer", return_value=data_layer),
            patch("chainlit.context.context_var"),
        ):
            init_http_context(user=user)

    await _assert_survives_gc(trigger, [update_gate])


async def test_flush_method_queue_task_survives_gc(mock_session):
    flush_gate = GatedWrite()
    mock_session.flush_method_queue = flush_gate
    emitter = ChainlitEmitter(mock_session)
    data_layer = AsyncMock()

    with patch("chainlit.emitter.get_data_layer", return_value=data_layer):
        await _assert_survives_gc(
            lambda: emitter.flush_thread_queues("hello"), [flush_gate]
        )


async def test_process_message_tasks_survive_gc(mock_session):
    create_gate = GatedWrite()
    init_gate = GatedWrite()
    elements_gate = GatedWrite()

    class FakeMessage:
        def __init__(self):
            self.id = str(uuid.uuid4())
            self.content = "hello"
            self.created_at = None
            self.elements = []

        @classmethod
        def from_dict(cls, _dict):
            return cls()

        async def _create(self):
            await create_gate()

    class FakeElement:
        @classmethod
        def from_dict(cls, _dict):
            return cls()

        @classmethod
        def infer_type_from_mime(cls, _mime):
            return "text"

        async def send(self, for_id):
            await elements_gate()

    mock_session.has_first_interaction = False
    mock_session.files = {
        "file-1": {
            "id": "file-1",
            "name": "doc.txt",
            "path": Path("/tmp/doc.txt"),
            "type": "text/plain",
        }
    }
    emitter = ChainlitEmitter(mock_session)
    emitter.init_thread = init_gate

    payload = {
        "message": {
            "id": str(uuid.uuid4()),
            "output": "hello",
            "createdAt": "2024-01-01T00:00:00Z",
        },
        "fileReferences": [{"id": "file-1"}],
    }

    with (
        patch("chainlit.emitter.Message", FakeMessage),
        patch("chainlit.emitter.Element", FakeElement),
        patch("chainlit.emitter.chat_context"),
    ):
        await _assert_survives_gc(
            lambda: emitter.process_message(payload),
            [create_gate, init_gate, elements_gate],
        )


async def test_registry_does_not_grow_with_completed_writes(mock_chainlit_context):
    async with mock_chainlit_context:
        registry_before = set(task_refs._persistence_tasks)
        data_layer = AsyncMock()

        with (
            patch("chainlit.message.get_data_layer", return_value=data_layer),
            patch("chainlit.message.chat_context"),
        ):
            for _ in range(50):
                await Message(content="hello").update()

        await _assert_registry_clean(registry_before)
        assert data_layer.update_step.await_count == 50
