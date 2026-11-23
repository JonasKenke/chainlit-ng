from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import AbstractAsyncContextManager
from typing import Any, AsyncIterator, Optional, Set

from chainlit.state_store.base import (
    LockNotAcquired,
    StateStore,
    StateStoreError,
)


class _MemoryLock(AbstractAsyncContextManager[None]):
    def __init__(
        self, store: MemoryStateStore, key: str, ttl: float, wait: float
    ) -> None:
        self._store = store
        self._key = key
        self._ttl = ttl
        self._wait = wait
        self._token = secrets.token_hex(16)

    async def __aenter__(self) -> None:
        if self._ttl <= 0:
            raise ValueError("lock ttl must be positive")
        deadline = time.monotonic() + max(self._wait, 0)
        while True:
            now = time.monotonic()
            held = self._store._locks.get(self._key)
            if held is None or held[1] <= now:
                self._store._locks[self._key] = (self._token, now + self._ttl)
                return None
            if self._wait <= 0:
                raise LockNotAcquired(f"Lock {self._key!r} is held")
            remaining = deadline - now
            if remaining <= 0:
                raise LockNotAcquired(f"Lock {self._key!r} is held")
            await asyncio.sleep(min(0.01, remaining))

    async def __aexit__(self, *exc_info: Any) -> None:
        held = self._store._locks.get(self._key)
        if held is not None and held[0] == self._token:
            self._store._locks.pop(self._key, None)


class MemoryStateStore(StateStore):
    """Process-local store. Values disappear on close and never leave the process."""

    def __init__(self, *, prefix: str = "chainlit") -> None:
        self._prefix = prefix
        self._values: dict[str, bytes | set[str]] = {}
        self._expires: dict[str, float] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[Optional[bytes]]]] = {}
        self._closed = False

    def _key(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("store key must be a non-empty string")
        return f"{self._prefix}:{key}" if self._prefix else key

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateStoreError("State store is closed")

    def _purge(self, key: str) -> None:
        expires_at = self._expires.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self._values.pop(key, None)
            self._expires.pop(key, None)

    @staticmethod
    def _check_ttl(ttl: Optional[float]) -> None:
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be positive or None")

    async def get(self, key: str) -> Optional[bytes]:
        self._ensure_open()
        full_key = self._key(key)
        self._purge(full_key)
        value = self._values.get(full_key)
        return value if isinstance(value, bytes) else None

    async def set(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        self._ensure_open()
        self._check_ttl(ttl)
        if not isinstance(value, bytes):
            raise TypeError("state store values must be bytes")
        full_key = self._key(key)
        self._values[full_key] = value
        if ttl is None:
            self._expires.pop(full_key, None)
        else:
            self._expires[full_key] = time.monotonic() + ttl

    async def set_if_absent(
        self, key: str, value: bytes, *, ttl: Optional[float] = None
    ) -> bool:
        self._ensure_open()
        self._check_ttl(ttl)
        if not isinstance(value, bytes):
            raise TypeError("state store values must be bytes")
        full_key = self._key(key)
        self._purge(full_key)
        if full_key in self._values:
            return False
        self._values[full_key] = value
        if ttl is None:
            self._expires.pop(full_key, None)
        else:
            self._expires[full_key] = time.monotonic() + ttl
        return True

    async def delete(self, *keys: str) -> None:
        self._ensure_open()
        for key in keys:
            full_key = self._key(key)
            self._values.pop(full_key, None)
            self._expires.pop(full_key, None)

    async def exists(self, key: str) -> bool:
        self._ensure_open()
        full_key = self._key(key)
        self._purge(full_key)
        return full_key in self._values

    async def expire(self, key: str, ttl: float) -> None:
        self._ensure_open()
        full_key = self._key(key)
        self._purge(full_key)
        if full_key not in self._values:
            return
        if ttl <= 0:
            self._values.pop(full_key, None)
            self._expires.pop(full_key, None)
        else:
            self._expires[full_key] = time.monotonic() + ttl

    async def set_add(
        self, key: str, member: str, *, ttl: Optional[float] = None
    ) -> None:
        self._ensure_open()
        self._check_ttl(ttl)
        if not isinstance(member, str):
            raise TypeError("set members must be strings")
        full_key = self._key(key)
        self._purge(full_key)
        current = self._values.get(full_key)
        if current is None:
            members: set[str] = set()
            self._values[full_key] = members
        elif isinstance(current, set):
            members = current
        else:
            raise StateStoreError(f"Key {key!r} is not a set")
        members.add(member)
        if ttl is not None:
            self._expires[full_key] = time.monotonic() + ttl

    async def set_remove(self, key: str, member: str) -> None:
        self._ensure_open()
        full_key = self._key(key)
        self._purge(full_key)
        current = self._values.get(full_key)
        if not isinstance(current, set):
            return
        current.discard(member)
        if not current:
            self._values.pop(full_key, None)
            self._expires.pop(full_key, None)

    async def set_members(self, key: str) -> Set[str]:
        self._ensure_open()
        full_key = self._key(key)
        self._purge(full_key)
        current = self._values.get(full_key)
        if current is None:
            return set()
        if not isinstance(current, set):
            raise StateStoreError(f"Key {key!r} is not a set")
        return set(current)

    async def increment(
        self, key: str, amount: int = 1, *, ttl: Optional[float] = None
    ) -> int:
        self._ensure_open()
        self._check_ttl(ttl)
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError("increment amount must be an integer")
        full_key = self._key(key)
        self._purge(full_key)
        current = self._values.get(full_key)
        if current is None:
            value = amount
            if ttl is not None:
                self._expires[full_key] = time.monotonic() + ttl
        elif isinstance(current, bytes):
            try:
                value = int(current) + amount
            except ValueError as exc:
                raise StateStoreError(f"Key {key!r} is not an integer") from exc
        else:
            raise StateStoreError(f"Key {key!r} is not an integer")
        self._values[full_key] = str(value).encode()
        return value

    def lock(
        self, key: str, *, ttl: float = 10, wait: float = 0
    ) -> AbstractAsyncContextManager[None]:
        self._ensure_open()
        return _MemoryLock(self, self._key(key), ttl, wait)

    async def publish(self, channel: str, value: bytes) -> None:
        self._ensure_open()
        if not isinstance(value, bytes):
            raise TypeError("state store messages must be bytes")
        full_channel = self._key(channel)
        for queue in tuple(self._subscribers.get(full_channel, ())):
            queue.put_nowait(value)

    async def subscribe(self, channel: str) -> AsyncIterator[bytes]:
        self._ensure_open()
        full_channel = self._key(channel)
        queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._subscribers.setdefault(full_channel, set()).add(queue)
        try:
            while True:
                value = await queue.get()
                if value is None:
                    return
                yield value
        finally:
            subscribers = self._subscribers.get(full_channel)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(full_channel, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._values.clear()
        self._expires.clear()
        self._locks.clear()
        for subscribers in self._subscribers.values():
            for queue in subscribers:
                queue.put_nowait(None)
        self._subscribers.clear()
