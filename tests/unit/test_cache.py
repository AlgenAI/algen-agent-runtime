from __future__ import annotations

import asyncio

from conftest import make_runtime

from algen_agent_runtime.cache import (
    CacheContext,
    CachePolicy,
    CacheScope,
    CacheService,
    InMemoryCacheStore,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.types.contracts import RunRequest


async def test_cache_isolates_tenants_and_users() -> None:
    cache = CacheService(
        InMemoryCacheStore(),
        {"results": CachePolicy(enabled=True, scope=CacheScope.USER)},
    )
    first = CacheContext(tenant_id="tenant-a", user_id="user-a")
    other_user = CacheContext(tenant_id="tenant-a", user_id="user-b")
    other_tenant = CacheContext(tenant_id="tenant-b", user_id="user-a")

    await cache.set_json("results", "query", {"q": 1}, {"value": 7}, first)

    assert await cache.get_json("results", "query", {"q": 1}, first) == {"value": 7}
    assert await cache.get_json("results", "query", {"q": 1}, other_user) is None
    assert await cache.get_json("results", "query", {"q": 1}, other_tenant) is None


async def test_cache_coalesces_concurrent_loaders() -> None:
    cache = CacheService(
        InMemoryCacheStore(),
        {"results": CachePolicy(enabled=True, scope=CacheScope.TENANT)},
    )
    context = CacheContext(tenant_id="tenant")
    calls = 0

    async def load() -> dict[str, int]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"value": 9}

    values = await asyncio.gather(
        *(cache.get_or_set_json("results", "query", "same", context, load) for _ in range(10))
    )

    assert calls == 1
    assert all(value == {"value": 9} for value, _ in values)
    assert sum(hit for _, hit in values) == 9


async def test_cache_tag_invalidation_and_size_limit() -> None:
    cache = CacheService(
        InMemoryCacheStore(),
        {
            "results": CachePolicy(
                enabled=True,
                scope=CacheScope.GLOBAL,
                maximum_value_bytes=20,
            )
        },
    )
    context = CacheContext()
    assert await cache.set_json(
        "results", "query", "a", {"value": 1}, context, tags=("dataset:v1",)
    )
    assert await cache.invalidate_tags("dataset:v1") == 1
    assert await cache.get_json("results", "query", "a", context) is None
    assert not await cache.set_json("results", "query", "large", {"value": "x" * 100}, context)


async def test_disabled_policy_never_stores() -> None:
    cache = CacheService(InMemoryCacheStore())
    context = CacheContext(tenant_id="tenant")
    assert not await cache.set_json("unknown", "query", "a", {"value": 1}, context)
    assert await cache.get_json("unknown", "query", "a", context) is None


async def test_runtime_reuses_exact_model_response_within_user_scope() -> None:
    provider = MockModelProvider(["answer"])
    runtime = make_runtime(provider=provider)
    runtime.cache = CacheService(
        InMemoryCacheStore(),
        {"model_responses": CachePolicy(enabled=True, scope=CacheScope.USER)},
    )
    request = RunRequest(
        agent="test-agent", input="same question", tenant_id="tenant", user_id="user"
    )

    first = await runtime.run(request)
    second = await runtime.run(request)

    assert first.output == second.output == "answer"
    assert len(provider.requests) == 1
    assert second.execution_summary.usage.cached_tokens > 0
