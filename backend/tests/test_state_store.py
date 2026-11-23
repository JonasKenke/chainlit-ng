import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from chainlit.config import StateStoreSettings
from chainlit.state_store import (
    MemoryStateStore,
    RedisStateStore,
    create_state_store,
    get_state_store,
    reset_state_store,
)
from chainlit.state_store.base import LockNotAcquired


@pytest_asyncio.fixture(params=["memory", "redis"])
async def store(request):
    prefix = f"chainlit-test:{uuid.uuid4().hex}"
    if request.param == "memory":
        value = MemoryStateStore(prefix=prefix)
    else:
        url = os.getenv("CHAINLIT_TEST_REDIS_URL")
        if not url:
            pytest.skip("CHAINLIT_TEST_REDIS_URL is not set")
        value = RedisStateStore(url, prefix=prefix)
    yield value
    await value.close()


async def test_set_and_get_round_trip(store):
    await store.set("key", b"value")

    assert await store.get("key") == b"value"
    assert await store.exists("key") is True
    assert await store.get("missing") is None
    assert await store.exists("missing") is False


async def test_set_if_absent_is_atomic(store):
    assert await store.set_if_absent("key", b"first") is True
    assert await store.set_if_absent("key", b"second") is False
    assert await store.get("key") == b"first"


async def test_ttl_expires_values(store):
    await store.set("key", b"value", ttl=0.02)

    await asyncio.sleep(0.05)

    assert await store.get("key") is None
    assert await store.exists("key") is False


async def test_expire_can_be_set_after_write(store):
    await store.set("key", b"value")

    await store.expire("key", 0.02)
    await asyncio.sleep(0.05)

    assert await store.get("key") is None


async def test_delete_removes_several_keys(store):
    await store.set("a", b"1")
    await store.set("b", b"2")
    await store.set("c", b"3")

    await store.delete("a", "b")

    assert await store.get("a") is None
    assert await store.get("b") is None
    assert await store.get("c") == b"3"


async def test_json_helpers_round_trip(store):
    payload = {"nested": ["value", 1, True, None]}

    await store.set_json("json", payload, ttl=30)

    assert await store.get_json("json") == payload
    assert await store.get_json("missing") is None


async def test_set_members_round_trip(store):
    await store.set_add("members", "a")
    await store.set_add("members", "b")

    assert await store.set_members("members") == {"a", "b"}

    await store.set_remove("members", "a")

    assert await store.set_members("members") == {"b"}
    assert await store.set_members("missing") == set()


async def test_set_ttl_expires_members(store):
    await store.set_add("members", "a", ttl=0.02)

    await asyncio.sleep(0.05)

    assert await store.set_members("members") == set()


async def test_increment_is_atomic(store):
    assert await store.increment("counter") == 1
    assert await store.increment("counter", 2) == 3
    assert await store.get_json("counter") == 3


async def test_counter_ttl_starts_at_first_increment(store):
    assert await store.increment("counter", ttl=0.03) == 1
    assert await store.increment("counter") == 2

    await asyncio.sleep(0.06)

    assert await store.increment("counter") == 1


async def test_lock_is_exclusive(store):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with store.lock("resource", ttl=10):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()

    with pytest.raises(LockNotAcquired):
        async with store.lock("resource", ttl=10):
            pass

    release.set()
    await task


async def test_lock_can_wait_for_release(store):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with store.lock("resource", ttl=10):
            entered.set()
            await release.wait()

    task = asyncio.create_task(holder())
    await entered.wait()

    release.set()

    async with store.lock("resource", ttl=10, wait=1):
        acquired = True

    await task

    assert acquired is True


async def test_lock_released_after_context_exits(store):
    async with store.lock("resource", ttl=10):
        pass

    async with store.lock("resource", ttl=10):
        acquired = True

    assert acquired is True


async def test_publish_reaches_subscriber(store):
    received = []

    async def subscribe():
        async for message in store.subscribe("channel"):
            received.append(message)
            break

    task = asyncio.create_task(subscribe())
    await asyncio.sleep(0.01)

    await store.publish("channel", b"payload")
    await asyncio.wait_for(task, timeout=1)

    assert received == [b"payload"]


async def test_publish_without_subscribers_is_a_noop(store):
    await store.publish("channel", b"payload")


async def test_prefixes_isolate_stores():
    first = MemoryStateStore(prefix="first")
    second = MemoryStateStore(prefix="second")

    await first.set("key", b"first-value")
    await second.set("key", b"second-value")

    assert await first.get("key") == b"first-value"
    assert await second.get("key") == b"second-value"


def test_redis_settings_require_a_url():
    with pytest.raises(ValueError, match="redis_url is required"):
        StateStoreSettings(backend="redis")


async def test_create_state_store_uses_memory_by_default():
    store = create_state_store(StateStoreSettings(prefix="test"))

    assert isinstance(store, MemoryStateStore)
    await store.close()


async def test_get_state_store_is_process_wide():
    reset_state_store()
    first = get_state_store()
    second = get_state_store()

    assert first is second

    reset_state_store()
    await first.close()


async def test_create_redis_store_from_settings():
    url = os.getenv("CHAINLIT_TEST_REDIS_URL")
    if not url:
        pytest.skip("CHAINLIT_TEST_REDIS_URL is not set")

    store = create_state_store(
        StateStoreSettings(backend="redis", redis_url=url, prefix="chainlit-test")
    )

    assert isinstance(store, RedisStateStore)
    assert await store.ping() is True
    await store.close()
