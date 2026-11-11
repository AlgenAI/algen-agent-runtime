"""Which calls inside a foreign framework the Runtime governs (#119).

A Runtime tool handed to a framework through GovernedToolBridge goes through the ToolExecutor:
permissions, input validation, policy and the approval refusal apply. A framework's own tools do
not. These tests pin that boundary, so a framework upgrade that bypasses it is caught.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import PolicyDeniedError, ToolExecutionError
from algen_agent_runtime.frameworks import (
    FrameworkRunRequest,
    GovernedToolBridge,
    framework_tool_name,
)
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

REQUEST = FrameworkRunRequest(
    input="Look up ACME",
    run_id="run-framework",
    session_id="session-a",
    tenant_id="tenant-a",
    user_id="user-a",
)


def _tool(
    name: str,
    calls: list[tuple[dict[str, Any], ToolContext]],
    *,
    permissions: frozenset[str] = frozenset(),
    requires_approval: bool = False,
) -> Tool:
    async def execute(arguments: dict[str, Any], context: ToolContext) -> Any:
        calls.append((arguments, context))
        return {"account": arguments["name"], "tier": "gold"}

    return Tool(
        ToolDefinition(
            name=name,
            version="1.0.0",
            description=f"{name} for the test",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False,
            },
            output_schema={},
            required_permissions=permissions,
            side_effect=SideEffect.WRITE if requires_approval else SideEffect.READ,
            requires_approval=requires_approval,
        ),
        execute,
    )


def _bridge(
    calls: list[tuple[dict[str, Any], ToolContext]], *, permissions: tuple[str, ...] = ("crm.read",)
) -> GovernedToolBridge:
    registry = ToolRegistry()
    registry.register(_tool("crm.find_account", calls, permissions=frozenset({"crm.read"})))
    registry.register(_tool("crm.close_account", calls, requires_approval=True))
    executor = ToolExecutor(registry, CompositePolicyEngine())
    return GovernedToolBridge(executor, registry, REQUEST, permissions=permissions)


async def test_a_bridged_call_runs_through_the_executor_with_the_run_identity() -> None:
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _bridge(calls)

    first = await bridge.call("crm.find_account", {"name": "ACME"})
    await bridge.call("crm.find_account", {"name": "Globex"})

    assert first == {"account": "ACME", "tier": "gold"}
    contexts = [context for _, context in calls]
    assert {(c.run_id, c.tenant_id, c.user_id) for c in contexts} == {
        ("run-framework", "tenant-a", "user-a")
    }
    # Each call has its own idempotency key, so the ledger never replays a different call.
    assert len({context.idempotency_key for context in contexts}) == 2


async def test_a_bridged_call_without_the_permission_is_refused() -> None:
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _bridge(calls, permissions=())

    with pytest.raises(PolicyDeniedError, match="missing permissions"):
        await bridge.call("crm.find_account", {"name": "ACME"})
    assert calls == []


async def test_a_tool_that_requires_approval_is_refused_through_the_bridge() -> None:
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _bridge(calls)

    with pytest.raises(PolicyDeniedError, match="requires an approval"):
        await bridge.call("crm.close_account", {"name": "ACME"})
    assert calls == []


async def test_invalid_input_from_the_framework_is_refused() -> None:
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _bridge(calls)

    with pytest.raises(ToolExecutionError, match="invalid input"):
        await bridge.call("crm.find_account", {"name": 7})
    assert calls == []


async def test_a_refusal_reaches_the_framework_model_as_text() -> None:
    bridge = _bridge([])

    text = await bridge.call_for_model("crm.close_account", {"name": "ACME"})

    assert json.loads(text)["error"] == "PolicyDeniedError"


def test_framework_tool_names_drop_characters_frameworks_refuse() -> None:
    assert framework_tool_name("crm.find_account") == "crm_find_account"


async def test_openai_agents_sdk_calls_go_through_the_bridge_and_native_tools_do_not() -> None:
    pytest.importorskip("agents")
    from agents import Agent, RunConfig, Runner, function_tool
    from agents.testing import ScriptedModel, assistant_message, function_call

    calls: list[tuple[dict[str, Any], ToolContext]] = []
    native_calls: list[str] = []
    bridge = _bridge(calls)

    @function_tool
    def native_lookup(name: str) -> str:
        """A tool the SDK runs itself."""
        native_calls.append(name)
        return "native"

    model = ScriptedModel(
        [
            [
                function_call("crm_find_account", {"name": "ACME"}, call_id="c1"),
                function_call("crm_close_account", {"name": "ACME"}, call_id="c2"),
                function_call("native_lookup", {"name": "ACME"}, call_id="c3"),
            ],
            [assistant_message("done")],
        ]
    )
    agent = Agent(
        name="analyst",
        model=model,
        tools=[*bridge.openai_agents_tools("crm.find_account", "crm.close_account"), native_lookup],
    )

    result = await Runner.run(agent, "Look up ACME", run_config=RunConfig(tracing_disabled=True))

    assert result.final_output == "done"
    # The bridged lookup ran through the executor; the approval-required close was refused there.
    assert [arguments for arguments, _ in calls] == [{"name": "ACME"}]
    # The SDK's own tool ran without the Runtime: not governed, only traced by the SDK.
    assert native_calls == ["ACME"]


async def test_langchain_tools_go_through_the_bridge() -> None:
    pytest.importorskip("langchain_core")
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _bridge(calls)
    [lookup, close] = bridge.langchain_tools("crm.find_account", "crm.close_account")

    found = await lookup.ainvoke({"name": "ACME"})
    refused = await close.ainvoke({"name": "ACME"})

    assert json.loads(found) == {"account": "ACME", "tier": "gold"}
    assert json.loads(refused)["error"] == "PolicyDeniedError"
    assert [arguments for arguments, _ in calls] == [{"name": "ACME"}]
