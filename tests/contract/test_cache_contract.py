from __future__ import annotations

from typing import Any

from algen_agent_runtime.cache import InMemoryCacheStore, RedisCacheStore


class FakeRedisPipeline:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def set(self, *args: Any, **kwargs: Any) -> FakeRedisPipeline:
        self.commands.append(("set", args, kwargs))
        return self

    def sadd(self, *args: Any, **kwargs: Any) -> FakeRedisPipeline:
        self.commands.append(("sadd", args, kwargs))
        return self

    def expire(self, *args: Any, **kwargs: Any) -> FakeRedisPipeline:
        self.commands.append(("expire", args, kwargs))
        return self

    async def execute(self) -> None:
        for method, args, kwargs in self.commands:
            await getattr(self.client, method)(*args, **kwargs)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str | bytes, bytes] = {}
        self.sets: dict[str, set[str | bytes]] = {}

    def pipeline(self, transaction: bool = True) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, **_: Any) -> None:
        self.values[key] = value

    async def sadd(self, key: str, value: str | bytes) -> None:
        self.sets.setdefault(key, set()).add(value)

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def smembers(self, key: str) -> set[str | bytes]:
        return self.sets.get(key, set())

    async def delete(self, *keys: str | bytes) -> None:
        for key in keys:
            self.values.pop(key, None)
            self.sets.pop(str(key), None)

    async def ping(self) -> bool:
        return True


async def test_in_memory_cache_store_contract() -> None:
    store = InMemoryCacheStore(max_entries=2)
    await store.set("one", b"1", 60, ("group",))
    await store.set("two", b"2", 60)
    assert await store.get("one") == b"1"
    await store.set("three", b"3", 60)
    assert await store.get("two") is None
    assert await store.invalidate_tags(("group",)) == 1
    assert await store.get("one") is None
    assert await store.health()


async def test_redis_cache_store_contract() -> None:
    client = FakeRedis()
    store = RedisCacheStore(client, "test")
    await store.set("one", b"1", 60, ("group",))
    assert await store.get("one") == b"1"
    assert await store.invalidate_tags(("group",)) == 1
    assert await store.get("one") is None
    assert await store.health()
