"""core.subagent: lineage, server-side depth, the delegate allowlist and no cached delegation."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import Idempotency
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.subagent import subagent_tool
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus, ToolPolicy

DELEGATING = {
    "enabled_tools": frozenset({"core.subagent"}),
    "tool_permissions": frozenset({"agents.delegate"}),
}


def _request(user: str = "alice", **updates: Any) -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id=user, **updates)


def _runtime(
    *responses: Any, max_depth: int = 3, child: dict[str, Any] | None = None, **parent: Any
) -> AgentRuntime:
    runtime = make_runtime(MockModelProvider(list(responses)), make_agent(**DELEGATING, **parent))
    runtime.agents.register(make_agent(name="child-agent", **(child or {})))
    runtime.tools.register(subagent_tool(runtime, max_depth=max_depth))
    return runtime


def _runs(runtime: AgentRuntime) -> list[RunState]:
    return list(runtime.runs._runs.values())  # type: ignore[attr-defined]


def _children(runtime: AgentRuntime, parent_run_id: str) -> list[RunState]:
    return [run for run in _runs(runtime) if run.request.parent_run_id == parent_run_id]


def test_delegation_is_volatile_and_never_retried() -> None:
    definition = subagent_tool(make_runtime()).definition

    assert definition.volatile
    assert definition.idempotency == Idempotency.NON_IDEMPOTENT


async def test_the_child_run_records_its_parent_in_parent_run_id() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        "child done",
        "parent done",
        delegates=frozenset({"child-agent"}),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    [child] = _children(runtime, result.run_id)
    assert child.request.agent == "child-agent"
    assert child.request.user_id == "alice"
    assert child.request.metadata["parent_run_id"] == result.run_id
    assert child.request.metadata["depth"] == "1"
    assert child.status == RunStatus.COMPLETED


async def test_an_agent_without_delegates_may_not_delegate() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        "parent done",
        tool_policy=ToolPolicy(on_error="return_to_model"),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert _children(runtime, result.run_id) == []
    assert len(_runs(runtime)) == 1


async def test_delegating_outside_the_allowlist_fails_the_run_by_default() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        delegates=frozenset({"other-agent"}),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert "may not delegate to 'child-agent'" in (result.error or "")
    assert _children(runtime, result.run_id) == []


async def test_a_wildcard_allows_any_agent() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        "child done",
        "parent done",
        delegates=frozenset({"*"}),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert len(_children(runtime, result.run_id)) == 1


async def test_a_child_with_wider_tool_permissions_is_refused() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        delegates=frozenset({"child-agent"}),
        child={"tool_permissions": frozenset({"crm.write"})},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert "crm.write" in (result.error or "")
    assert _children(runtime, result.run_id) == []


async def test_a_child_within_the_parents_permissions_runs() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        "child done",
        "parent done",
        delegates=frozenset({"child-agent"}),
        child={"tool_permissions": frozenset({"agents.delegate"})},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error


async def test_the_model_cannot_reset_the_nesting_depth() -> None:
    # The agent delegates to itself and the model always claims depth 0: the Runtime counts the
    # lineage, so the child (depth 1) may not delegate again with max_depth=1.
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "test-agent", "input": "a", "depth": 0}),
        tool_call_response("core.subagent", {"agent": "test-agent", "input": "b", "depth": 0}),
        "parent done",
        max_depth=1,
        delegates=frozenset({"test-agent"}),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    [child] = _children(runtime, result.run_id)
    assert child.status == RunStatus.FAILED
    assert "nesting limit 1 reached" in (child.error or "")
    assert _children(runtime, child.id) == []


async def test_a_workflow_lineage_does_not_count_as_delegation() -> None:
    # Workflow nodes chain parent_run_id from node to node; only delegated runs add depth.
    runtime = _runtime(
        "first node",
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        "child done",
        "second node",
        max_depth=1,
        delegates=frozenset({"child-agent"}),
    )
    first = await runtime.run(_request())

    second = await runtime.run(_request(parent_run_id=first.run_id))

    assert second.status == RunStatus.COMPLETED, second.error
    [child] = _children(runtime, second.run_id)
    assert child.status == RunStatus.COMPLETED


async def test_forged_delegation_metadata_on_a_root_run_adds_no_depth() -> None:
    runtime = _runtime(
        tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
        "child done",
        "parent done",
        max_depth=1,
        delegates=frozenset({"child-agent"}),
    )

    result = await runtime.run(_request(metadata={"delegation": "core.subagent", "depth": "9"}))

    assert result.status == RunStatus.COMPLETED, result.error
    [child] = _children(runtime, result.run_id)
    assert child.status == RunStatus.COMPLETED


async def test_a_delegation_is_never_served_from_the_tool_result_cache() -> None:
    provider = MockModelProvider(
        [
            tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
            "alice's child",
            "alice done",
            tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
            "bob's child",
            "bob done",
        ]
    )
    runtime = make_runtime(provider, make_agent(**DELEGATING, delegates=frozenset({"child-agent"})))
    runtime.agents.register(make_agent(name="child-agent"))
    cache = CacheService(
        InMemoryCacheStore(),
        {"tool_results": CachePolicy(enabled=True, scope=CacheScope.TENANT)},
    )
    runtime.tool_executor = ToolExecutor(runtime.tools, runtime.policies, cache=cache)
    runtime.tools.register(subagent_tool(runtime))

    alice = await runtime.run(_request("alice"))
    bob = await runtime.run(_request("bob"))

    assert alice.status == bob.status == RunStatus.COMPLETED
    assert len(provider.requests) == 6  # Bob's child ran: nothing was served from the cache
    [bob_child] = _children(runtime, bob.run_id)
    assert bob_child.request.user_id == "bob"
    assert bob_child.output_text == "bob's child"


def test_delegates_must_be_agent_names_or_a_wildcard() -> None:
    assert make_agent(delegates=frozenset({"child-agent", "*"})).delegates == {"child-agent", "*"}
    with pytest.raises(ValidationError, match="delegates must be agent names"):
        make_agent(delegates=frozenset({"Not An Agent"}))
