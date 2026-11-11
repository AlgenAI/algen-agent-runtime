"""A volatile tool is never stored in, or served from, the tool-result cache."""

from __future__ import annotations

from typing import Any

from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

CONTEXT = ToolContext(
    run_id="run-1",
    step_id="step-1",
    tenant_id="tenant-a",
    user_id="alice",
    permissions=frozenset(),
    idempotency_key="k0",
)


def _executor(volatile: bool) -> tuple[ToolExecutor, InMemoryCacheStore, list[int]]:
    calls: list[int] = []

    async def status(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(len(calls))
        return {"status": f"poll-{len(calls)}"}

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="jobs.status",
                version="1.0.0",
                description="A job's current status",
                input_schema={"type": "object", "properties": {"job": {"type": "string"}}},
                output_schema={"type": "object"},
                volatile=volatile,
            ),
            status,
        )
    )
    store = InMemoryCacheStore()
    cache = CacheService(
        store, {"tool_results": CachePolicy(enabled=True, scope=CacheScope.TENANT)}
    )
    return ToolExecutor(registry, CompositePolicyEngine(), cache=cache), store, calls


async def _twice(executor: ToolExecutor) -> list[Any]:
    return [
        (
            await executor.execute(
                "jobs.status",
                {"job": "j1"},
                CONTEXT.model_copy(update={"idempotency_key": key}),
            )
        ).value
        for key in ("k1", "k2")
    ]


async def test_a_volatile_tool_runs_every_time_and_stores_nothing() -> None:
    executor, store, calls = _executor(volatile=True)

    values = await _twice(executor)

    assert values == [{"status": "poll-1"}, {"status": "poll-2"}]
    assert len(calls) == 2
    assert not store._items  # type: ignore[attr-defined]


async def test_a_stable_read_is_still_cached() -> None:
    executor, store, calls = _executor(volatile=False)

    values = await _twice(executor)

    assert values == [{"status": "poll-1"}, {"status": "poll-1"}]
    assert len(calls) == 1
    assert store._items  # type: ignore[attr-defined]
