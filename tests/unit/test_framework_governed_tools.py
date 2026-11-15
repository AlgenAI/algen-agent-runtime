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
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import ApprovalPolicy

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


def _side_effect_tool(
    name: str, side_effect: SideEffect, calls: list[tuple[dict[str, Any], ToolContext]]
) -> Tool:
    async def execute(arguments: dict[str, Any], context: ToolContext) -> Any:
        calls.append((arguments, context))
        return {"done": arguments}

    return Tool(
        ToolDefinition(
            name=name,
            version="1.0.0",
            description=name,
            input_schema={"type": "object"},
            output_schema={},
            side_effect=side_effect,
        ),
        execute,
    )


def _side_effect_bridge(
    calls: list[tuple[dict[str, Any], ToolContext]], **options: Any
) -> GovernedToolBridge:
    registry = ToolRegistry()
    for side_effect in SideEffect:
        registry.register(_side_effect_tool(f"t.{side_effect.value}", side_effect, calls))
    executor = ToolExecutor(registry, CompositePolicyEngine())
    return GovernedToolBridge(executor, registry, REQUEST, **options)


@pytest.mark.parametrize("side_effect", ["write", "external", "destructive"])
async def test_a_call_a_runtime_run_would_pause_for_is_refused_by_default(side_effect: str) -> None:
    # The bridge applies plan_tool's approval rule, so a framework cannot run a state-changing
    # tool that the default ApprovalPolicy would gate in a Runtime run.
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _side_effect_bridge(calls)

    with pytest.raises(PolicyDeniedError, match="requires an approval"):
        await bridge.call(f"t.{side_effect}", {"table": "payroll"})
    assert calls == []


async def test_read_tools_run_and_a_permissive_approval_policy_allows_the_rest() -> None:
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    strict = _side_effect_bridge(calls)
    permissive = _side_effect_bridge(
        calls,
        approval_policy=ApprovalPolicy(
            require_for_side_effects=False, require_for_destructive=False
        ),
    )

    await strict.call("t.read", {})
    await strict.call("t.none", {})
    await permissive.call("t.destructive", {"table": "tmp"})

    assert [arguments for arguments, _ in calls] == [{}, {}, {"table": "tmp"}]


class _PlanPolicy:
    category = "test"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.PLAN_TOOL})

    def __init__(self, name: str, decision: PolicyDecision) -> None:
        self.name = name
        self.decision = decision
        self.seen: list[Any] = []

    async def evaluate(self, point: str, payload: Any, context: Any) -> PolicyDecision:
        self.seen.append((point, payload, context["tool"].name, context["tenant_id"]))
        return self.decision


async def test_the_policy_engine_is_evaluated_at_plan_tool() -> None:
    deny = _PlanPolicy(
        "no_reads",
        PolicyDecision(action=PolicyAction.DENY, reason_code="test.denied", reason="No."),
    )
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _side_effect_bridge(calls, policies=CompositePolicyEngine((deny,)))

    with pytest.raises(PolicyDeniedError, match=r"test\.denied"):
        await bridge.call("t.read", {"q": 1})

    assert calls == []
    assert deny.seen == [("plan_tool", {"q": 1}, "t.read", "tenant-a")]


async def test_a_plan_tool_transform_rewrites_the_arguments() -> None:
    rewrite = _PlanPolicy(
        "limit",
        PolicyDecision(
            action=PolicyAction.TRANSFORM,
            reason_code="test.limited",
            reason="Limited.",
            value={"q": 1, "limit": 10},
        ),
    )
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _side_effect_bridge(calls, policies=CompositePolicyEngine((rewrite,)))

    await bridge.call("t.read", {"q": 1})

    assert [arguments for arguments, _ in calls] == [{"q": 1, "limit": 10}]


async def test_the_mandatory_side_effect_policy_follows_the_bridges_approval_policy() -> None:
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    bridge = _side_effect_bridge(
        calls,
        policies=CompositePolicyEngine(),
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
    )

    await bridge.call("t.write", {"row": 1})
    with pytest.raises(PolicyDeniedError, match="requires an approval"):
        await bridge.call("t.destructive", {"row": 1})

    assert [arguments for arguments, _ in calls] == [{"row": 1}]


async def test_two_bridges_for_one_run_never_replay_each_others_calls() -> None:
    # The idempotency key used to be a per-bridge counter: a second bridge for the same run got
    # the first bridge's recorded result for its call 1, whatever its arguments.
    calls: list[tuple[dict[str, Any], ToolContext]] = []
    registry = ToolRegistry()
    registry.register(_tool("crm.find_account", calls, permissions=frozenset({"crm.read"})))
    executor = ToolExecutor(registry, CompositePolicyEngine())
    first = GovernedToolBridge(executor, registry, REQUEST, permissions={"crm.read"})
    second = GovernedToolBridge(executor, registry, REQUEST, permissions={"crm.read"})

    acme = await first.call("crm.find_account", {"name": "ACME"})
    globex = await second.call("crm.find_account", {"name": "Globex"})

    assert acme["account"] == "ACME"
    assert globex["account"] == "Globex"
    assert len({context.idempotency_key for _, context in calls}) == 2
