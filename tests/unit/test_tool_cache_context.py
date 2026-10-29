"""The tool-result cache keys a result on the ToolContext values its tool declares."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from pydantic import ValidationError

from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.orchestration.describe import _tool as describe_tool
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.authoring import build_tool, read
from algen_agent_runtime.tools.contracts import Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

BASE = ToolContext(
    run_id="run-1",
    step_id="step-1",
    tenant_id="tenant-a",
    user_id="alice",
    permissions=frozenset(),
    idempotency_key="k0",
)


def _context(key: str, **changes: str) -> ToolContext:
    return BASE.model_copy(update={"idempotency_key": key, **changes})


def _setup(
    cache_context: frozenset[Any] = frozenset(),
) -> tuple[ToolExecutor, CacheService, list[Any]]:
    """A read tool that answers with who asked, behind a tenant-scoped tool-result cache."""
    calls: list[Any] = []

    async def mine(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(context)
        return {"user": context.user_id, "run": context.run_id, "step": context.step_id}

    definition = ToolDefinition(
        name="sql.my_orders",
        version="1.0.0",
        description="The caller's orders",
        input_schema={"type": "object", "properties": {"status": {"type": "string"}}},
        output_schema={"type": "object"},
        cache_context=cache_context,
    )
    registry = ToolRegistry()
    registry.register(Tool(definition, mine))
    cache = CacheService(
        InMemoryCacheStore(), {"tool_results": CachePolicy(enabled=True, scope=CacheScope.TENANT)}
    )
    return ToolExecutor(registry, CompositePolicyEngine(), cache=cache), cache, calls


ARGS = {"status": "open"}


async def test_a_declared_user_id_keeps_users_of_one_tenant_apart() -> None:
    executor, _, calls = _setup(frozenset({"user_id"}))

    alice = await executor.execute("sql.my_orders", ARGS, _context("k1"))
    bob = await executor.execute("sql.my_orders", ARGS, _context("k2", user_id="bob"))
    alice_again = await executor.execute("sql.my_orders", ARGS, _context("k3"))

    assert alice.value["user"] == "alice"
    assert bob.value["user"] == "bob"
    assert not bob.metadata.get("runtime_cache_hit")
    assert alice_again.metadata.get("runtime_cache_hit") is True
    assert len(calls) == 2


async def test_without_a_declaration_the_tenant_shares_results_as_before() -> None:
    executor, _, calls = _setup()

    await executor.execute("sql.my_orders", ARGS, _context("k1"))
    bob = await executor.execute("sql.my_orders", ARGS, _context("k2", user_id="bob"))

    assert bob.metadata.get("runtime_cache_hit") is True
    assert bob.value["user"] == "alice"
    assert len(calls) == 1


@pytest.mark.parametrize(("field", "other"), [("run_id", "run-2"), ("step_id", "step-2")])
async def test_a_declared_run_or_step_keys_on_it(field: str, other: str) -> None:
    executor, _, calls = _setup(frozenset({field}))

    await executor.execute("sql.my_orders", ARGS, _context("k1"))
    same = await executor.execute("sql.my_orders", ARGS, _context("k2", user_id="bob"))
    moved = await executor.execute("sql.my_orders", ARGS, _context("k3", **{field: other}))

    # Only the declared value takes part: another user in the same run and step still hits.
    assert same.metadata.get("runtime_cache_hit") is True
    assert not moved.metadata.get("runtime_cache_hit")
    assert len(calls) == 2


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def test_the_key_of_a_tool_declaring_nothing_is_unchanged() -> None:
    executor, cache, _ = _setup()
    await executor.execute("sql.my_orders", ARGS, _context("k1"))

    # The key exactly as it was before cache_context existed, so stored entries stay valid.
    material = json.dumps(
        {"tool": "sql.my_orders", "version": "1.0.0", "arguments": ARGS},
        sort_keys=True,
        separators=(",", ":"),
    )
    expected = f"v1:tool_results:tool.result:{_digest('tenant-a')}:{_digest(material)}"
    assert list(cache.store._items) == [expected]  # type: ignore[attr-defined]


async def test_declared_values_never_appear_in_the_key() -> None:
    executor, cache, _ = _setup(frozenset({"user_id", "run_id", "step_id"}))
    await executor.execute("sql.my_orders", ARGS, _context("k1"))

    [key] = cache.store._items  # type: ignore[attr-defined]
    assert not any(value in key for value in ("alice", "run-1", "step-1"))


def test_cache_context_round_trips_and_is_validated() -> None:
    definition = ToolDefinition(
        name="sql.my_orders",
        version="1",
        description="d",
        input_schema={"type": "object"},
        output_schema={},
        cache_context=frozenset({"user_id", "run_id"}),
    )

    dumped = definition.model_dump(mode="json")
    assert sorted(dumped["cache_context"]) == ["run_id", "user_id"]
    assert ToolDefinition.model_validate(dumped) == definition
    assert ToolDefinition.model_validate_json(definition.model_dump_json()) == definition
    with pytest.raises(ValidationError, match="cache_context"):
        ToolDefinition.model_validate({**dumped, "cache_context": ["permissions"]})


def test_build_tool_and_describe_carry_cache_context() -> None:
    async def my_orders(status: str, context: ToolContext) -> list[str]:
        """The caller's orders."""
        return []

    built = build_tool(my_orders, read(), cache_context={"user_id"})
    assert built.definition.cache_context == frozenset({"user_id"})
    assert build_tool(my_orders, read()).definition.cache_context == frozenset()
    assert describe_tool(built, "manifest", ()).cache_context == ("user_id",)


async def test_a_call_with_caller_credentials_still_bypasses_the_cache() -> None:
    executor, cache, calls = _setup(frozenset({"user_id"}))
    signed_in = {"credentials": {"authorization": "Bearer t"}}

    await executor.execute("sql.my_orders", ARGS, _context("k1").model_copy(update=signed_in))
    await executor.execute("sql.my_orders", ARGS, _context("k2").model_copy(update=signed_in))

    assert len(calls) == 2
    assert len(cache.store._items) == 0  # type: ignore[attr-defined]
