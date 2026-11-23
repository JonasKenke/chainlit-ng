from __future__ import annotations

from typing import Optional

from chainlit.config import StateStoreSettings, config
from chainlit.state_store.base import StateStore
from chainlit.state_store.memory import MemoryStateStore
from chainlit.state_store.redis import RedisStateStore

_state_store: Optional[StateStore] = None


def create_state_store(settings: Optional[StateStoreSettings] = None) -> StateStore:
    """Build a store from settings without caching it."""
    store_settings = settings or config.project.state_store
    if store_settings.backend == "memory":
        return MemoryStateStore(prefix=store_settings.prefix)
    if not store_settings.redis_url:
        raise ValueError("state_store.redis_url is required for the redis backend")
    return RedisStateStore(store_settings.redis_url, prefix=store_settings.prefix)


def get_state_store() -> StateStore:
    """Return the process-wide store, creating it on first use."""
    global _state_store
    if _state_store is None:
        _state_store = create_state_store()
    return _state_store


async def close_state_store() -> None:
    """Close and forget the process-wide store."""
    global _state_store
    store, _state_store = _state_store, None
    if store is not None:
        await store.close()


def reset_state_store() -> None:
    """Forget the process-wide store without closing it. For tests."""
    global _state_store
    _state_store = None
