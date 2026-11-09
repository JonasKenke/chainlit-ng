import pytest

from chainlit.data.base import BaseDataLayer


async def test_delete_all_threads_is_not_implemented_by_default():
    with pytest.raises(NotImplementedError, match="Bulk thread deletion"):
        await BaseDataLayer.delete_all_threads(None, "user-id")  # type: ignore[arg-type]


class _MinimalLayer(BaseDataLayer):
    """A layer that implements nothing new; the ACL defaults must deny."""

    async def get_user(self, identifier):
        return None

    async def create_user(self, user):
        return None

    async def delete_feedback(self, feedback_id):
        return False

    async def upsert_feedback(self, feedback):
        return ""

    async def create_element(self, element):
        pass

    async def get_element(self, thread_id, element_id):
        return None

    async def delete_element(self, element_id, thread_id=None):
        pass

    async def create_step(self, step_dict):
        pass

    async def update_step(self, step_dict):
        pass

    async def delete_step(self, step_id):
        pass

    async def get_thread_author(self, thread_id):
        raise ValueError(thread_id)

    async def delete_thread(self, thread_id):
        pass

    async def list_threads(self, pagination, filters):
        return None

    async def get_thread(self, thread_id):
        return None

    async def update_thread(
        self, thread_id, name=None, user_id=None, metadata=None, tags=None
    ):
        pass

    async def close(self):
        pass

    async def get_favorite_steps(self, user_id):
        return []


async def test_grant_methods_are_opt_in_and_fail_closed():
    layer = _MinimalLayer()

    assert layer.thread_grants_supported is False
    assert layer.feedback_authorization_supported is False
    assert await layer.get_thread_grant("thread-1", "user-1") is None
    for coro in (
        layer.list_thread_grants("thread-1"),
        layer.create_thread_grant(None),  # type: ignore[arg-type]
        layer.delete_thread_grant("thread-1", "user-1"),
        layer.get_feedback_thread_id("feedback-1"),
    ):
        with pytest.raises(NotImplementedError):
            await coro


async def test_step_belongs_to_thread_denies_on_any_uncertainty(monkeypatch):
    layer = _MinimalLayer()

    async def steps_thread(thread_id):
        return {"id": thread_id, "steps": [{"id": "step-1"}]}

    monkeypatch.setattr(layer, "get_thread", steps_thread)
    assert await layer.step_belongs_to_thread("thread-1", "step-1") is True
    assert await layer.step_belongs_to_thread("thread-1", "step-2") is False

    async def failing(thread_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(layer, "get_thread", failing)
    assert await layer.step_belongs_to_thread("thread-1", "step-1") is False
