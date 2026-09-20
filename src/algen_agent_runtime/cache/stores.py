from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict
from typing import Any


class NullCacheStore:
    backend_name = "none"

    async def get(self, key: str) -> bytes | None:
        return None

    async def set(
        self, key: str, value: bytes, ttl_seconds: int, tags: tuple[str, ...] = ()
    ) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def invalidate_tags(self, tags: tuple[str, ...]) -> int:
        return 0

    async def health(self) -> bool:
        return True


class InMemoryCacheStore:
    """Bounded LRU cache for local development and single-process deployments."""

    backend_name = "memory"

    def __init__(self, max_entries: int = 10_000) -> None:
        self._max_entries = max_entries
        self._items: OrderedDict[str, tuple[float, bytes, tuple[str, ...]]] = OrderedDict()
        self._tags: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value, tags = item
            if expires_at <= time.monotonic():
                self._remove(key, tags)
                return None
            self._items.move_to_end(key)
            return value

    async def set(
        self, key: str, value: bytes, ttl_seconds: int, tags: tuple[str, ...] = ()
    ) -> None:
        async with self._lock:
            prior = self._items.pop(key, None)
            if prior:
                self._remove_tags(key, prior[2])
            self._items[key] = (time.monotonic() + ttl_seconds, value, tags)
            for tag in tags:
                self._tags[tag].add(key)
            while len(self._items) > self._max_entries:
                old_key, (_, _, old_tags) = self._items.popitem(last=False)
                self._remove_tags(old_key, old_tags)

    async def delete(self, key: str) -> None:
        async with self._lock:
            item = self._items.pop(key, None)
            if item:
                self._remove_tags(key, item[2])

    async def invalidate_tags(self, tags: tuple[str, ...]) -> int:
        async with self._lock:
            keys = set().union(*(self._tags.get(tag, set()) for tag in tags)) if tags else set()
            for key in keys:
                item = self._items.pop(key, None)
                if item:
                    self._remove_tags(key, item[2])
            return len(keys)

    async def health(self) -> bool:
        return True

    def _remove(self, key: str, tags: tuple[str, ...]) -> None:
        self._items.pop(key, None)
        self._remove_tags(key, tags)

    def _remove_tags(self, key: str, tags: tuple[str, ...]) -> None:
        for tag in tags:
            members = self._tags.get(tag)
            if members is not None:
                members.discard(key)
                if not members:
                    self._tags.pop(tag, None)


class RedisCacheStore:
    """Redis cache with tag sets for bounded invalidation."""

    backend_name = "redis"

    def __init__(self, client: Any, prefix: str = "algen-agent-runtime:cache") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":")

    def _value_key(self, key: str) -> str:
        return f"{self._prefix}:value:{key}"

    def _tag_key(self, tag: str) -> str:
        return f"{self._prefix}:tag:{tag}"

    async def get(self, key: str) -> bytes | None:
        value = await self._client.get(self._value_key(key))
        if value is None:
            return None
        return value.encode() if isinstance(value, str) else bytes(value)

    async def set(
        self, key: str, value: bytes, ttl_seconds: int, tags: tuple[str, ...] = ()
    ) -> None:
        value_key = self._value_key(key)
        pipeline = self._client.pipeline(transaction=True)
        pipeline.set(value_key, value, ex=ttl_seconds)
        for tag in tags:
            tag_key = self._tag_key(tag)
            pipeline.sadd(tag_key, value_key)
            pipeline.expire(tag_key, ttl_seconds)
        await pipeline.execute()

    async def delete(self, key: str) -> None:
        await self._client.delete(self._value_key(key))

    async def invalidate_tags(self, tags: tuple[str, ...]) -> int:
        keys: set[bytes | str] = set()
        for tag in tags:
            keys.update(await self._client.smembers(self._tag_key(tag)))
        if keys:
            await self._client.delete(*keys)
        if tags:
            await self._client.delete(*(self._tag_key(tag) for tag in tags))
        return len(keys)

    async def health(self) -> bool:
        return bool(await self._client.ping())
