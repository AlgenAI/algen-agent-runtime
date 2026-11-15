"""core.subagent never widens what a run may do, and a delegation tree shares its budget
(GOV-6, GOV-7)."""

from __future__ import annotations

import json
from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.subagent import delegated_agent, subagent_tool
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    Budget,
    FinishReason,
    GuardrailPolicy,
    Message,
    ModelResponse,
    PIIGuardrail,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    TokenUsage,
    ToolPolicy,
)

DELEGATION = tool_call_response("core.subagent", {"agent": "child-agent", "input": "do it"})
UPDATE = tool_call_response("crm.update", {"id": "1"})
SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


def _runtime(
    *responses: Any,
    parent: dict[str, Any] | None = None,
    child: dict[str, Any] | None = None,
    max_descendants: int = 64,
) -> tuple[AgentRuntime, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    runtime = make_runtime(
        MockModelProvider(list(responses)),
        make_agent(
            **{
                "enabled_tools": frozenset({"core.subagent", "crm.update"}),
                "tool_permissions": frozenset({"agents.delegate", "crm.write"}),
                "delegates": frozenset({"child-agent"}),
                **(parent or {}),
            }
        ),
    )
    runtime.agents.register(
        make_agent(
            **{
                "name": "child-agent",
                "enabled_tools": frozenset({"crm.update"}),
                "tool_permissions": frozenset({"crm.write"}),
                **(child or {}),
            }
        )
    )

    def update(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"updated": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="crm.update",
                version="1",
                description="Update a CRM record",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.WRITE,
                required_permissions=frozenset({"crm.write"}),
            ),
            update,
        )
    )
    runtime.tools.register(subagent_tool(runtime, max_descendants=max_descendants))
    return runtime, calls


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="alice")


def _children(runtime: AgentRuntime, parent_run_id: str) -> list[RunState]:
    runs = runtime.runs._runs.values()  # type: ignore[attr-defined]
    return [run for run in runs if run.request.parent_run_id == parent_run_id]


def _answer(text: str, input_tokens: int = 10) -> ModelResponse:
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, text),
        finish_reason=FinishReason.STOP,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=5),
        model="deterministic",
        provider="mock",
    )


async def test_a_child_that_enables_a_tool_its_parent_does_not_is_refused() -> None:
    runtime, calls = _runtime(
        DELEGATION,
        "parent done",
        parent={
            "enabled_tools": frozenset({"core.subagent"}),
            "tool_policy": ToolPolicy(on_error="return_to_model"),
        },
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert _children(runtime, result.run_id) == []
    state = await runtime.status(result.run_id, "t")
    [refusal] = [m for m in state.messages if m.role == Role.TOOL and m.name == "core.subagent"]
    assert "enables tools 'test-agent' does not: ['crm.update']" in refusal.text_content
    assert calls == []


async def test_a_child_runs_with_its_parents_approval_requirements() -> None:
    # The child would write without asking; its parent requires approval for side effects.
    runtime, calls = _runtime(
        DELEGATION,
        UPDATE,
        child={"approval_policy": ApprovalPolicy(require_for_side_effects=False)},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.AWAITING_APPROVAL, result.error
    [child] = _children(runtime, result.run_id)
    assert child.status == RunStatus.AWAITING_APPROVAL
    assert calls == []


async def test_without_a_stricter_parent_the_childs_own_approvals_apply() -> None:
    runtime, calls = _runtime(
        DELEGATION,
        UPDATE,
        "child done",
        "parent done",
        parent={"approval_policy": ApprovalPolicy(require_for_side_effects=False)},
        child={"approval_policy": ApprovalPolicy(require_for_side_effects=False)},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert calls == [{"id": "1"}]


async def test_a_child_runs_with_its_parents_guardrail_policies() -> None:
    # The child turns every guardrail off; the parent's secret redaction still applies.
    runtime, _ = _runtime(
        DELEGATION,
        f"the key is api_key={SECRET}",
        "parent done",
        child={"guardrail_policy": GuardrailPolicy(policies=())},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    [child] = _children(runtime, result.run_id)
    assert SECRET not in (child.output_text or "")
    assert "[REDACTED]" in (child.output_text or "")


def test_delegated_agent_merges_the_parents_floor_only_for_a_delegated_run() -> None:
    parent = make_agent(
        guardrail_policy=GuardrailPolicy(
            policies=("secrets", "pii"),
            tool_result_injection="block",
            pii=PIIGuardrail(exempt_entities=frozenset({"email"})),
        )
    )
    child = make_agent(
        name="child-agent",
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
        guardrail_policy=GuardrailPolicy(
            policies=("pii",),
            tool_policies=("pii",),
            pii=PIIGuardrail(exempt_entities=frozenset({"email", "phone"})),
        ),
    )
    floor = json.dumps(
        {
            "require_for_side_effects": True,
            "require_for_destructive": True,
            "policies": list(parent.guardrail_policy.policies),
            "tool_policies": ["secrets"],
            "tool_result_injection": "block",
            "retrieval_injection": "off",
            "pii_exempt": ["email"],
        }
    )
    request = RunRequest(
        agent="child-agent",
        input="x",
        tenant_id="t",
        user_id="u",
        parent_run_id="run-parent",
        metadata={"delegation": "core.subagent", "delegation_floor": floor},
    )
    merged = delegated_agent(child, RunState(request=request, agent_key=child.key))

    assert merged.approval_policy.require_for_side_effects
    assert merged.guardrail_policy.policies == ("pii", "secrets")
    assert merged.guardrail_policy.tool_policies == ("pii", "secrets")
    assert merged.guardrail_policy.tool_result_injection == "block"
    assert merged.guardrail_policy.pii.exempt_entities == {"email"}
    # Not delegated (no parent): the floor in metadata is ignored.
    root = request.model_copy(update={"parent_run_id": None})
    assert delegated_agent(child, RunState(request=root, agent_key=child.key)) == child


async def test_a_childs_usage_and_tool_calls_are_charged_to_its_parent() -> None:
    runtime, _ = _runtime(
        DELEGATION,
        UPDATE,
        _answer("child done", input_tokens=40),
        _answer("parent done", input_tokens=10),
        parent={"approval_policy": ApprovalPolicy(require_for_side_effects=False)},
        child={"approval_policy": ApprovalPolicy(require_for_side_effects=False)},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    parent = await runtime.status(result.run_id, "t")
    [child] = _children(runtime, result.run_id)
    assert child.summary.usage.input_tokens == 40
    assert child.summary.tool_calls == 1
    assert parent.summary.usage.input_tokens == 50
    assert parent.summary.tool_calls == 2  # its own delegation and the child's update


async def test_a_parent_whose_children_used_its_budget_stops() -> None:
    runtime, _ = _runtime(
        DELEGATION,
        _answer("child done", input_tokens=5_000),
        "parent would go on",
        parent={"budget": Budget(max_tokens=1_000)},
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    assert result.limit_reached == "model_tokens"


async def test_a_delegation_tree_starts_at_most_max_descendants_children() -> None:
    runtime, _ = _runtime(
        DELEGATION,
        "child one done",
        DELEGATION,
        "parent done",
        parent={"tool_policy": ToolPolicy(on_error="return_to_model")},
        max_descendants=1,
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert len(_children(runtime, result.run_id)) == 1
    state = await runtime.status(result.run_id, "t")
    refusals = [
        m.text_content
        for m in state.messages
        if m.role == Role.TOOL and m.name == "core.subagent" and "limit" in m.text_content
    ]
    assert refusals and "max_descendants is 1" in refusals[0]
