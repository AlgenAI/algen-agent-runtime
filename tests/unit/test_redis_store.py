from __future__ import annotations

import fnmatch
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.persistence.redis import RedisMemoryStore, RedisRunStore
from algen_agent_runtime.types.contracts import (
    Message,
    Role,
    RunRequest,
    RunState,
    RunStatus,
)

pytestmark = [pytest.mark.redis]


class FakeRedisPipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._watched_keys: set[str] = set()
        self._in_multi = False

    async def __aenter__(self) -> FakeRedisPipeline:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._commands.clear()
        self._watched_keys.clear()
        self._in_multi = False

    async def watch(self, *keys: str) -> None:
        self._watched_keys.update(keys)

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    def multi(self) -> None:
        self._in_multi = True

    def set(self, key: str, value: str, **kwargs: Any) -> None:
        self._commands.append(("set", (key, value), kwargs))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for cmd, args, kwargs in self._commands:
            if cmd == "set":
                res = await self._redis.set(args[0], args[1], **kwargs)
                results.append(res)
        self._commands.clear()
        self._in_multi = False
        return results


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        val = self.data.get(key)
        return val if isinstance(val, str) else None

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for k in keys:
            if k in self.data:
                del self.data[k]
                self.ttls.pop(k, None)
                deleted += 1
        return deleted

    async def rpush(self, key: str, *values: str) -> int:
        if key not in self.data:
            self.data[key] = []
        self.data[key].extend(values)
        return len(self.data[key])

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        items: list[str] = self.data.get(key, [])
        if stop == -1:
            return items[start:]
        return items[start : stop + 1]

    async def expire(self, key: str, seconds: int) -> bool:
        if key in self.data:
            self.ttls[key] = seconds
            return True
        return False

    async def scan_iter(self, match: str = "*", count: int = 1000) -> AsyncIterator[str]:
        for key in list(self.data.keys()):
            if fnmatch.fnmatch(key, match):
                yield key

    def pipeline(self, transaction: bool = True) -> FakeRedisPipeline:
        return FakeRedisPipeline(self)


def _make_run(
    run_id: str = "run-1",
    tenant_id: str = "tenant-a",
    status: RunStatus = RunStatus.PLANNING,
    version: int = 0,
) -> RunState:
    return RunState(
        id=run_id,
        request=RunRequest(
            agent="test-agent",
            input="hello",
            tenant_id=tenant_id,
            user_id="user-1",
        ),
        status=status,
        version=version,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_redis_run_store_crud_and_tenant_isolation() -> None:
    redis = FakeRedis()
    store = RedisRunStore(redis, prefix="test-algen")

    run_a = _make_run("run-1", "tenant-a")
    run_b = _make_run("run-1", "tenant-b")

    # Create run_a
    await store.create(run_a)

    # Duplicate run_a in same tenant raises ConflictError
    with pytest.raises(ConflictError, match="already exists"):
        await store.create(run_a)

    # Same run_id in tenant-b succeeds (tenant isolation)
    await store.create(run_b)

    # Retrieval respects tenant
    fetched_a = await store.get("run-1", "tenant-a")
    assert fetched_a is not None
    assert fetched_a.id == "run-1"
    assert fetched_a.request.tenant_id == "tenant-a"

    fetched_b = await store.get("run-1", "tenant-b")
    assert fetched_b is not None
    assert fetched_b.id == "run-1"
    assert fetched_b.request.tenant_id == "tenant-b"

    # Non-existent run returns None
    assert await store.get("non-existent", "tenant-a") is None


@pytest.mark.asyncio
async def test_redis_run_store_optimistic_concurrency_control() -> None:
    redis = FakeRedis()
    store = RedisRunStore(redis)

    run = _make_run("run-1", "tenant-a", version=0)
    await store.create(run)

    # Save with matching expected_version succeeds and increments version
    loaded = await store.get("run-1", "tenant-a")
    assert loaded is not None
    assert loaded.version == 0

    await store.save(loaded, expected_version=0)
    assert loaded.version == 1

    reloaded = await store.get("run-1", "tenant-a")
    assert reloaded is not None
    assert reloaded.version == 1

    # Save with stale expected_version raises ConflictError
    with pytest.raises(ConflictError, match="concurrently modified"):
        await store.save(loaded, expected_version=0)


@pytest.mark.asyncio
async def test_redis_run_store_list_active_filters_terminal_states() -> None:
    redis = FakeRedis()
    store = RedisRunStore(redis)

    active1 = _make_run("run-1", "tenant-a", status=RunStatus.PLANNING)
    active2 = _make_run("run-2", "tenant-a", status=RunStatus.RECEIVED)
    completed = _make_run("run-3", "tenant-a", status=RunStatus.COMPLETED)
    failed = _make_run("run-4", "tenant-a", status=RunStatus.FAILED)

    await store.create(active1)
    await store.create(active2)
    await store.create(completed)
    await store.create(failed)

    active_runs = await store.list_active()
    active_ids = {r.id for r in active_runs}
    assert active_ids == {"run-1", "run-2"}


@pytest.mark.asyncio
async def test_redis_memory_store_append_get_delete_and_expiry() -> None:
    redis = FakeRedis()
    store = RedisMemoryStore(redis, prefix="test-algen", retention_seconds=3600)

    msg1 = Message.text(Role.USER, "Hello")
    msg2 = Message.text(Role.ASSISTANT, "Hi there!")
    msg3 = Message.text(Role.USER, "How are you?")

    # Append messages
    await store.append("tenant-a", "session-1", [msg1, msg2])
    await store.append("tenant-a", "session-1", [msg3])

    # Key TTL was set
    expected_key = "test-algen:tenant:tenant-a:session:session-1:memory"
    assert redis.ttls.get(expected_key) == 3600

    # Retrieve all
    retrieved = await store.get("tenant-a", "session-1", limit=10)
    assert len(retrieved) == 3
    assert retrieved[0].text_content == "Hello"
    assert retrieved[1].text_content == "Hi there!"
    assert retrieved[2].text_content == "How are you?"

    # Retrieve with limit
    limited = await store.get("tenant-a", "session-1", limit=2)
    assert len(limited) == 2
    assert limited[0].text_content == "Hi there!"
    assert limited[1].text_content == "How are you?"

    # Tenant and session isolation
    assert await store.get("tenant-b", "session-1", limit=10) == ()
    assert await store.get("tenant-a", "session-2", limit=10) == ()

    # Delete
    await store.delete("tenant-a", "session-1")
    assert await store.get("tenant-a", "session-1", limit=10) == ()
