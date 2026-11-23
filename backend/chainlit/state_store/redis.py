from __future__ import annotations

import asyncio
import secrets
from contextlib import AbstractAsyncContextManager
from typing import Any, AsyncIterator, Optional, Set

from chainlit.state_store.base import (
    LockNotAcquired,
    StateStore,
    StateStoreError,
)

_INCREMENT_SCRIPT = """
local exists = redis.call('EXISTS', KEYS[1])
local value = redis.call('INCRBY', KEYS[1], ARGV[1])
if tonumber(ARGV[2]) > 0 and exists == 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return value
"""

_SET_ADD_SCRIPT = """
local added = redis.call('SADD', KEYS[1], ARGV[1])
if tonumber(ARGV[2]) > 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return added
"""

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _ttl_ms(ttl: Optional[float]) -> Optional[int]:
    if ttl is None:
        return None
    if ttl <= 0:
        raise ValueError("ttl must be positive or None")
    return max(1, int(ttl * 1000))


class _RedisLock(AbstractAsyncContextManager[None]):
    def __init__(
        self, store: RedisStateStore, key: str, ttl: float, wait: float
    ) -> None:
        self._store = store
        self._key = key
        self._ttl = ttl
        self._wait = wait
        self._token = secrets.token_hex(16)
        self._ttl_ms = _ttl_ms(ttl)
        assert self._ttl_ms is not None

    async def __aenter__(self) -> None:
        deadline = asyncio.get_running_loop().time() + max(self._wait, 0)
        while True:
            acquired = await self._store._redis.set(
                self._key, self._token.encode(), nx=True, px=self._ttl_ms
            )
            if acquired:
                return None
            if self._wait <= 0:
                raise LockNotAcquired(f"Lock {self._key!r} is held")
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise LockNotAcquired(f"Lock {self._key!r} is held")
            await asyncio.sleep(min(0.01, remaining))

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._store._redis.eval(_RELEASE_LOCK_SCRIPT, 1, self._key, self._token)


class RedisStateStore(StateStore):
    """Redis-backed store for coordination across workers.

    Requires the ``redis`` extra: ``pip install "chainlit-ng[redis]"``.
    The lock is a single-key lock, not a Redlock quorum. Multi-key atomicity is
    not provided, so a Redis Cluster needs all related keys in one hash slot.
    """

    shared = True

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "chainlit",
        socket_connect_timeout: Optional[float] = None,
        socket_timeout: Optional[float] = None,
    ) -> None:
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - dependency is optional
            raise StateStoreError(
                'RedisStateStore requires the "redis" extra: '
                'pip install "chainlit-ng[redis]"'
            ) from exc
        self._redis: Any = Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
        )
        self._prefix = prefix
        self._closed = False

    def _key(self, key: str) -> str:
        if not isinstance(key, str) or not key:
            raise ValueError("store key must be a non-empty string")
        return f"{self._prefix}:{key}" if self._prefix else key

    def _ensure_open(self) -> None:
        if self._closed:
            raise StateStoreError("State store is closed")

    async def ping(self) -> bool:
        self._ensure_open()
        return bool(await self._redis.ping())

    async def get(self, key: str) -> Optional[bytes]:
        self._ensure_open()
        return await self._redis.get(self._key(key))

    async def set(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        self._ensure_open()
        if not isinstance(value, bytes):
            raise TypeError("state store values must be bytes")
        await self._redis.set(self._key(key), value, px=_ttl_ms(ttl))

    async def set_if_absent(
        self, key: str, value: bytes, *, ttl: Optional[float] = None
    ) -> bool:
        self._ensure_open()
        if not isinstance(value, bytes):
            raise TypeError("state store values must be bytes")
        result = await self._redis.set(self._key(key), value, nx=True, px=_ttl_ms(ttl))
        return bool(result)

    async def delete(self, *keys: str) -> None:
        self._ensure_open()
        if keys:
            await self._redis.delete(*(self._key(key) for key in keys))

    async def exists(self, key: str) -> bool:
        self._ensure_open()
        return bool(await self._redis.exists(self._key(key)))

    async def expire(self, key: str, ttl: float) -> None:
        self._ensure_open()
        full_key = self._key(key)
        if ttl <= 0:
            await self._redis.delete(full_key)
        else:
            await self._redis.pexpire(full_key, _ttl_ms(ttl))

    async def set_add(
        self, key: str, member: str, *, ttl: Optional[float] = None
    ) -> None:
        self._ensure_open()
        if not isinstance(member, str):
            raise TypeError("set members must be strings")
        await self._redis.eval(
            _SET_ADD_SCRIPT,
            1,
            self._key(key),
            member,
            str(_ttl_ms(ttl) or 0),
        )

    async def set_remove(self, key: str, member: str) -> None:
        self._ensure_open()
        await self._redis.srem(self._key(key), member)

    async def set_members(self, key: str) -> Set[str]:
        self._ensure_open()
        members = await self._redis.smembers(self._key(key))
        return {
            member.decode() if isinstance(member, bytes) else str(member)
            for member in members
        }

    async def increment(
        self, key: str, amount: int = 1, *, ttl: Optional[float] = None
    ) -> int:
        self._ensure_open()
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError("increment amount must be an integer")
        value = await self._redis.eval(
            _INCREMENT_SCRIPT,
            1,
            self._key(key),
            str(amount),
            str(_ttl_ms(ttl) or 0),
        )
        return int(value)

    def lock(
        self, key: str, *, ttl: float = 10, wait: float = 0
    ) -> AbstractAsyncContextManager[None]:
        self._ensure_open()
        return _RedisLock(self, self._key(key), ttl, wait)

    async def publish(self, channel: str, value: bytes) -> None:
        self._ensure_open()
        if not isinstance(value, bytes):
            raise TypeError("state store messages must be bytes")
        await self._redis.publish(self._key(channel), value)

    async def subscribe(self, channel: str) -> AsyncIterator[bytes]:
        self._ensure_open()
        pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
        full_channel = self._key(channel)
        await pubsub.subscribe(full_channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                value = message.get("data")
                if isinstance(value, bytes):
                    yield value
                elif value is not None:
                    yield str(value).encode()
        finally:
            await pubsub.unsubscribe(full_channel)
            await pubsub.aclose()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._redis.aclose()
