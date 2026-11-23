from __future__ import annotations

import json
from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from typing import Any, AsyncIterator, Optional, Set


class StateStoreError(Exception):
    """Base error for state store failures."""


class LockNotAcquired(StateStoreError):
    """Raised when a lock cannot be acquired within its wait budget."""


class StateStore(ABC):
    """Ephemeral, namespaced state with TTLs, atomic primitives, and pub/sub.

    Values are bytes. TTLs are seconds. The store is a coordination layer,
    not a durable replacement for the data layer: a process crash, Redis
    restart, or TTL expiry may lose entries.
    """

    @abstractmethod
    async def get(self, key: str) -> Optional[bytes]:
        """Return the value, or None when missing or expired."""

    @abstractmethod
    async def set(self, key: str, value: bytes, *, ttl: Optional[float] = None) -> None:
        """Set a value, replacing any existing value and TTL."""

    @abstractmethod
    async def set_if_absent(
        self, key: str, value: bytes, *, ttl: Optional[float] = None
    ) -> bool:
        """Set a value only when the key does not exist. Returns success."""

    @abstractmethod
    async def delete(self, *keys: str) -> None:
        """Delete one or more keys. Missing keys are ignored."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """True when the key exists and has not expired."""

    @abstractmethod
    async def expire(self, key: str, ttl: float) -> None:
        """Set a TTL on an existing key. A non-positive TTL deletes it."""

    @abstractmethod
    async def set_add(
        self, key: str, member: str, *, ttl: Optional[float] = None
    ) -> None:
        """Add a member to a set, creating it when missing."""

    @abstractmethod
    async def set_remove(self, key: str, member: str) -> None:
        """Remove a member from a set. Missing keys and members are ignored."""

    @abstractmethod
    async def set_members(self, key: str) -> Set[str]:
        """Return a snapshot of a set's members."""

    @abstractmethod
    async def increment(
        self, key: str, amount: int = 1, *, ttl: Optional[float] = None
    ) -> int:
        """Atomically add to an integer, creating it at zero when missing."""

    @abstractmethod
    def lock(
        self, key: str, *, ttl: float = 10, wait: float = 0
    ) -> AbstractAsyncContextManager[None]:
        """Acquire an exclusive lock. Raises LockNotAcquired on timeout."""

    @abstractmethod
    async def publish(self, channel: str, value: bytes) -> None:
        """Publish a message to subscribers of a channel."""

    @abstractmethod
    def subscribe(self, channel: str) -> AsyncIterator[bytes]:
        """Yield messages published to a channel until the iterator closes."""

    @abstractmethod
    async def close(self) -> None:
        """Release backend resources. The store is unusable afterwards."""

    async def get_json(self, key: str) -> Any:
        value = await self.get(key)
        if value is None:
            return None
        return json.loads(value)

    async def set_json(
        self, key: str, value: Any, *, ttl: Optional[float] = None
    ) -> None:
        await self.set(
            key,
            json.dumps(
                value, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode(),
            ttl=ttl,
        )
