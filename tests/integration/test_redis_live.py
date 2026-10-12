from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from algen_agent_runtime.cache.stores import RedisCacheStore
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.persistence.redis import RedisMemoryStore, RedisRunStore
from algen_agent_runtime.types.contracts import (
    Message,
    Role,
    RunRequest,
    RunState,
    RunStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.redis,
    pytest.mark.skipif(not os.environ.get("REDIS_URL"), reason="requires a live Redis"),
]


@pytest.fixture
async def redis_client() -> AsyncIterator[Any]:
    asyncio_redis = pytest.importorskip("redis.asyncio")
    client = asyncio_redis.Redis.from_url(os.environ["REDIS_URL"])
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def prefix() -> str:
    # Unique per test so runs against a shared Redis never observe each other's keys.
    return f"algen-test:{uuid4()}"


def _run(tenant_id: str, status: RunStatus = RunStatus.PLANNING) -> RunState:
    return RunState(
        request=RunRequest(agent="test-agent", input="q", tenant_id=tenant_id, user_id="u"),
        status=status,
    )


async def test_run_store_round_trip_and_tenant_isolation(redis_client: Any, prefix: str) -> None:
    store = RedisRunStore(redis_client, prefix=prefix)
    state = _run("tenant-a")

    await store.create(state)
    with pytest.raises(ConflictError):
        await store.create(state)

    loaded = await store.get(state.id, "tenant-a")
    assert loaded is not None and loaded.id == state.id
    assert await store.get(state.id, "tenant-b") is None

    loaded.status = RunStatus.COMPLETED
    await store.save(loaded, expected_version=0)
    assert loaded.version == 1
    saved = await store.get(state.id, "tenant-a")
    assert saved is not None
    assert (saved.version, saved.status) == (1, RunStatus.COMPLETED)

    with pytest.raises(ConflictError):
        await store.save(saved, expected_version=0)


class _InterleavingRedis:
    """Proxy that writes the watched key from another connection between WATCH and EXEC."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        pipeline = self._client.pipeline(*args, **kwargs)
        client = self._client
        original_get = pipeline.get

        async def get_then_interfere(key: str) -> Any:
            value = await original_get(key)
            await client.set(key, value)  # same bytes, but WATCH still sees a modification
            return value

        pipeline.get = get_then_interfere
        return pipeline


async def test_run_store_maps_watch_conflicts_to_conflict_error(
    redis_client: Any, prefix: str
) -> None:
    state = _run("tenant-a")
    await RedisRunStore(redis_client, prefix=prefix).create(state)
    store = RedisRunStore(_InterleavingRedis(redis_client), prefix=prefix)

    with pytest.raises(ConflictError):
        await store.save(state.model_copy(update={"step_count": 1}), expected_version=0)

    current = await RedisRunStore(redis_client, prefix=prefix).get(state.id, "tenant-a")
    assert current is not None and current.version == 0


async def test_run_store_concurrent_saves_have_one_winner(redis_client: Any, prefix: str) -> None:
    store = RedisRunStore(redis_client, prefix=prefix)
    state = _run("tenant-a")
    await store.create(state)

    async def save(step: int) -> str:
        candidate = state.model_copy(update={"step_count": step}, deep=True)
        try:
            await store.save(candidate, expected_version=0)
        except ConflictError:
            return "conflict"
        return "saved"

    outcomes = await asyncio.gather(*(save(step) for step in range(1, 21)))

    assert outcomes.count("saved") == 1
    assert outcomes.count("conflict") == 19
    final = await store.get(state.id, "tenant-a")
    assert final is not None and final.version == 1


async def test_run_store_lists_only_active_runs(redis_client: Any, prefix: str) -> None:
    store = RedisRunStore(redis_client, prefix=prefix)
    active = _run("tenant-a", RunStatus.AWAITING_APPROVAL)
    other_tenant = _run("tenant-b", RunStatus.INVOKING_MODEL)
    for state in (active, other_tenant, _run("tenant-a", RunStatus.COMPLETED)):
        await store.create(state)

    listed = await store.list_active()

    assert {state.id for state in listed} == {active.id, other_tenant.id}


async def test_memory_store_sets_ttl_and_isolates_sessions(redis_client: Any, prefix: str) -> None:
    store = RedisMemoryStore(redis_client, prefix=prefix, retention_seconds=60)
    first = [Message.text(Role.USER, "hello"), Message.text(Role.ASSISTANT, "hi")]

    await store.append("tenant-a", "session-1", first)
    await store.append("tenant-a", "session-1", [Message.text(Role.USER, "again")])

    history = await store.get("tenant-a", "session-1", limit=2)
    assert [message.text_content for message in history] == ["hi", "again"]
    assert await store.get("tenant-b", "session-1", limit=10) == ()
    ttl = await redis_client.ttl(f"{prefix}:tenant:tenant-a:session:session-1:memory")
    assert 0 < ttl <= 60

    await store.delete("tenant-a", "session-1")
    assert await store.get("tenant-a", "session-1", limit=10) == ()


async def test_cache_store_ttl_and_tag_invalidation(redis_client: Any, prefix: str) -> None:
    store = RedisCacheStore(redis_client, prefix=f"{prefix}:cache")

    await store.set("a", b"one", ttl_seconds=60, tags=("tenant:t",))
    await store.set("b", b"two", ttl_seconds=60, tags=("tenant:t", "source:x"))
    await store.set("c", b"three", ttl_seconds=60)

    assert await store.get("a") == b"one"
    assert await store.health() is True
    assert 0 < await redis_client.ttl(f"{prefix}:cache:value:a") <= 60

    assert await store.invalidate_tags(("tenant:t",)) == 2
    assert await store.get("a") is None
    assert await store.get("b") is None
    assert await store.get("c") == b"three"
