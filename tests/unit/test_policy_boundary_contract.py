"""The policy actions each boundary supports (#116), one test per row of the table in
docs/operations.md ("Actions per boundary").

A policy answers at one boundary; the run must then do exactly what the table says: carry on,
use the policy's value, pause for approval, refuse the tool call or fail the run.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from pydantic import BaseModel

from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    Artifact,
    AttachmentPolicy,
    FileBlock,
    FinishReason,
    GuardrailPolicy,
    Message,
    ModelResponse,
    Role,
    RunAttachment,
    RunRequest,
    RunStatus,
    ToolCall,
    ToolPolicy,
)

TENANT = "t"
MARK = "MARKED"
CHANGED = "CHANGED"
ACTIONS = list(PolicyAction)
# Boundaries every run of the harness crosses: a tool call, then an answer saved to memory.
RUN_POINTS = [point for point in PolicyPoint if point is not PolicyPoint.ATTACHMENTS]


def _replace(value: Any) -> Any:
    """``value`` with MARK replaced by CHANGED in every string, at any depth."""
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        return value.replace(MARK, CHANGED)
    if isinstance(value, BaseModel):
        return value.model_copy(
            update={name: _replace(getattr(value, name)) for name in type(value).model_fields}
        )
    if isinstance(value, Mapping):
        return {key: _replace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace(item) for item in value)
    return value


class Decides:
    """Answers ``action`` at ``points``; a redact or transform replaces MARK with CHANGED."""

    name = "decides"
    category = "test"
    enforcement_mode = "block"

    def __init__(self, action: PolicyAction, *points: PolicyPoint) -> None:
        self.action = action
        self.applies_to = frozenset(points)

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        value = None
        if self.action in {PolicyAction.REDACT, PolicyAction.TRANSFORM}:
            value = _replace(payload)
        return PolicyDecision(action=self.action, reason_code="test", reason="test", value=value)


def _echo(side_effect: SideEffect = SideEffect.READ) -> Tool:
    calls: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"value": arguments["key"]}

    tool = Tool(
        ToolDefinition(
            name="echo",
            version="1",
            description="Echo a key.",
            side_effect=side_effect,
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            output_schema={"type": "object"},
        ),
        handler,
    )
    tool.calls = calls  # type: ignore[attr-defined]
    return tool


def _runtime(
    policy: Decides, side_effect: SideEffect = SideEffect.READ, **agent: Any
) -> tuple[AgentRuntime, Tool, MockModelProvider]:
    provider = MockModelProvider([tool_call_response("echo", {"key": MARK}), f"answer {MARK}"])
    tool = _echo(side_effect)
    agent.setdefault("guardrail_policy", GuardrailPolicy(policies=("decides",)))
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"echo"}), **agent))
    runtime.tools.register(tool)
    runtime.policies.register(policy)
    return runtime, tool, provider


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input=f"look up {MARK}", tenant_id=TENANT, user_id="u")


# What each boundary does with each action. "value" means the policy's value is used.
CONTINUES = "continues"
VALUE = "value"
PAUSES = "pauses"
REFUSED = "refused"
FAILS = "fails"
_VALUE_BOUNDARY = {
    PolicyAction.ALLOW: CONTINUES,
    PolicyAction.DENY: FAILS,
    PolicyAction.REDACT: VALUE,
    PolicyAction.TRANSFORM: VALUE,
    PolicyAction.REQUIRE_APPROVAL: FAILS,
    PolicyAction.REQUIRE_CLARIFICATION: FAILS,
}
CONTRACT: dict[PolicyPoint, dict[PolicyAction, str]] = {
    PolicyPoint.INPUT: _VALUE_BOUNDARY,
    PolicyPoint.BEFORE_RETRIEVAL: _VALUE_BOUNDARY,
    PolicyPoint.AFTER_RETRIEVAL: _VALUE_BOUNDARY,
    PolicyPoint.BEFORE_MODEL: _VALUE_BOUNDARY,
    PolicyPoint.AFTER_MODEL: _VALUE_BOUNDARY,
    PolicyPoint.PLAN_TOOL: {
        PolicyAction.ALLOW: CONTINUES,
        PolicyAction.DENY: REFUSED,
        PolicyAction.REDACT: REFUSED,
        PolicyAction.TRANSFORM: VALUE,
        PolicyAction.REQUIRE_APPROVAL: PAUSES,
        PolicyAction.REQUIRE_CLARIFICATION: REFUSED,
    },
    PolicyPoint.BEFORE_TOOL: {
        PolicyAction.ALLOW: CONTINUES,
        PolicyAction.DENY: REFUSED,
        PolicyAction.REDACT: REFUSED,
        PolicyAction.TRANSFORM: VALUE,
        PolicyAction.REQUIRE_APPROVAL: REFUSED,
        PolicyAction.REQUIRE_CLARIFICATION: REFUSED,
    },
    PolicyPoint.AFTER_TOOL: {
        PolicyAction.ALLOW: CONTINUES,
        PolicyAction.DENY: REFUSED,
        PolicyAction.REDACT: VALUE,
        PolicyAction.TRANSFORM: VALUE,
        PolicyAction.REQUIRE_APPROVAL: REFUSED,
        PolicyAction.REQUIRE_CLARIFICATION: REFUSED,
    },
    PolicyPoint.FINAL_RESPONSE: _VALUE_BOUNDARY,
    PolicyPoint.BEFORE_MEMORY_WRITE: _VALUE_BOUNDARY,
    # The payload is only a count of what was written: a changed value has no effect.
    PolicyPoint.AFTER_MEMORY_WRITE: {
        **_VALUE_BOUNDARY,
        PolicyAction.REDACT: CONTINUES,
        PolicyAction.TRANSFORM: CONTINUES,
    },
    PolicyPoint.ATTACHMENTS: _VALUE_BOUNDARY,
}


def test_the_contract_covers_every_boundary_and_action() -> None:
    assert set(CONTRACT) == set(PolicyPoint)
    assert all(set(row) == set(PolicyAction) for row in CONTRACT.values())


async def _value_used(
    point: PolicyPoint, runtime: AgentRuntime, tool: Tool, provider: MockModelProvider, run: Any
) -> bool:
    """Whether the policy's CHANGED value is what the run went on with at ``point``."""
    first_request = provider.requests[0].model_dump_json()
    if point in {
        PolicyPoint.INPUT,
        PolicyPoint.BEFORE_RETRIEVAL,
        PolicyPoint.AFTER_RETRIEVAL,
        PolicyPoint.BEFORE_MODEL,
    }:
        return CHANGED in first_request and MARK not in first_request
    if point in {PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL}:
        return tool.calls == [{"key": CHANGED}]  # type: ignore[attr-defined]
    if point == PolicyPoint.AFTER_TOOL:
        # The model reads the changed result; the tool itself ran with what the model sent.
        assert tool.calls == [{"key": MARK}]  # type: ignore[attr-defined]
        [result] = [m for m in provider.requests[1].messages if m.role.value == "tool"]
        return CHANGED in result.text_content and MARK not in result.text_content
    if point in {PolicyPoint.AFTER_MODEL, PolicyPoint.FINAL_RESPONSE}:
        return run.output == f"answer {CHANGED}"
    if point == PolicyPoint.BEFORE_MEMORY_WRITE:
        state = await runtime.status(run.run_id, TENANT)
        remembered = str(await runtime.memory.get(TENANT, state.session_id, 100))
        return CHANGED in remembered and MARK not in remembered
    raise AssertionError(point)


@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("point", RUN_POINTS)
async def test_each_boundary_does_what_the_table_says(
    point: PolicyPoint, action: PolicyAction
) -> None:
    expected = CONTRACT[point][action]
    runtime, tool, provider = _runtime(Decides(action, point))

    run = await runtime.run(_request())

    if expected == FAILS:
        assert run.status == RunStatus.FAILED
        return
    if expected == PAUSES:
        assert run.status == RunStatus.AWAITING_APPROVAL
        assert tool.calls == []  # type: ignore[attr-defined]
        return
    if expected == REFUSED:
        # The call is refused (the agent's default on_error fails the run); it never ran, or,
        # at after_tool, its result never reached the model.
        assert run.status == RunStatus.FAILED
        if point in {PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL}:
            assert tool.calls == []  # type: ignore[attr-defined]
        else:
            assert len(provider.requests) == 1
        return
    assert run.status == RunStatus.COMPLETED, run.error
    if expected == CONTINUES:
        assert tool.calls == [{"key": MARK}]  # type: ignore[attr-defined]
        assert run.output == f"answer {MARK}"
        return
    assert await _value_used(point, runtime, tool, provider, run)


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x02\x08\x02\x00\x00\x00"
    b"\xfd\xd4\x9as\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _DropsFiles(Decides):
    """At ``attachments`` a redact or transform keeps no file."""

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        decision = await super().evaluate(point, payload, context)
        if decision.value is None:
            return decision
        return decision.model_copy(update={"value": []})


@pytest.mark.parametrize("action", ACTIONS)
async def test_the_attachments_boundary_does_what_the_table_says(action: PolicyAction) -> None:
    expected = CONTRACT[PolicyPoint.ATTACHMENTS][action]
    store = InMemoryArtifactStore()
    image = Artifact(tenant_id=TENANT, name="a.png", media_type="image/png", data=PNG)
    await store.put(image)
    provider = MockModelProvider(["seen"])
    runtime = make_runtime(
        provider,
        make_agent(
            attachments=AttachmentPolicy(enabled=True),
            guardrail_policy=GuardrailPolicy(policies=("decides",)),
        ),
    )
    runtime.artifacts = store
    runtime.policies.register(_DropsFiles(action, PolicyPoint.ATTACHMENTS))

    run = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="what is it",
            tenant_id=TENANT,
            user_id="u",
            attachments=(RunAttachment(artifact_id=image.id),),
        )
    )

    if expected == FAILS:
        assert run.status == RunStatus.FAILED
        return
    assert run.status == RunStatus.COMPLETED
    files = [
        block
        for message in provider.requests[0].messages
        for block in message.content
        if isinstance(block, FileBlock)
    ]
    # A policy may only drop files: the value it returns is the subset the run keeps.
    assert len(files) == (1 if expected == CONTINUES else 0)


# before_tool rewrites and approval binding.


async def test_a_plan_tool_rewrite_is_what_the_approval_shows_and_what_runs() -> None:
    policy = Decides(PolicyAction.TRANSFORM, PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL)
    runtime, tool, _ = _runtime(policy, SideEffect.EXTERNAL)

    run = await runtime.run(_request())

    # side_effects asks for approval; the transform from the other policy still stands.
    assert run.status == RunStatus.AWAITING_APPROVAL
    state = await runtime.status(run.run_id, TENANT)
    assert state.pause_payload is not None
    assert state.pause_payload["redacted_parameters"] == {"key": CHANGED}
    [approval] = await runtime.approvals.list(TENANT, run_id=run.run_id)
    assert approval.redacted_parameters == {"key": CHANGED}

    await runtime.resume(run.run_id, TENANT, {"decision": "approved"})
    resumed = await runtime.wait(run.run_id, TENANT)

    assert resumed.status == RunStatus.COMPLETED, resumed.error
    assert tool.calls == [{"key": CHANGED}]  # type: ignore[attr-defined]


async def test_a_before_tool_rewrite_of_an_approved_call_is_refused() -> None:
    # Applies only at before_tool, so the approval covered the arguments the model sent.
    runtime, tool, _ = _runtime(
        Decides(PolicyAction.TRANSFORM, PolicyPoint.BEFORE_TOOL), SideEffect.EXTERNAL
    )

    run = await runtime.run(_request())
    assert run.status == RunStatus.AWAITING_APPROVAL
    await runtime.resume(run.run_id, TENANT, {"decision": "approved"})
    resumed = await runtime.wait(run.run_id, TENANT)

    assert resumed.status == RunStatus.FAILED
    assert "approval does not cover" in (resumed.error or "")
    assert tool.calls == []  # type: ignore[attr-defined]


async def test_a_redaction_with_an_approval_requirement_refuses_before_anyone_is_asked() -> None:
    runtime, tool, _ = _runtime(
        Decides(PolicyAction.REDACT, PolicyPoint.PLAN_TOOL), SideEffect.EXTERNAL
    )

    run = await runtime.run(_request())

    assert run.status == RunStatus.FAILED
    assert list(await runtime.approvals.list(TENANT, run_id=run.run_id)) == []
    assert tool.calls == []  # type: ignore[attr-defined]


class _BadRewrite(Decides):
    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        return PolicyDecision(
            action=PolicyAction.TRANSFORM, reason_code="bad", reason="bad", value={"key": 7}
        )


@pytest.mark.parametrize("point", [PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL])
async def test_rewritten_arguments_must_fit_the_input_schema(point: PolicyPoint) -> None:
    runtime, tool, _ = _runtime(_BadRewrite(PolicyAction.TRANSFORM, point))

    run = await runtime.run(_request())

    assert run.status == RunStatus.FAILED
    assert "policy-transformed input for echo is invalid" in (run.error or "")
    assert tool.calls == []  # type: ignore[attr-defined]


async def test_a_rewrite_in_a_parallel_reply_runs_the_rewritten_call() -> None:
    reply = ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=(
            ToolCall(id="call-1", name="echo", arguments={"key": MARK}),
            ToolCall(id="call-2", name="echo", arguments={"key": "other"}),
        ),
        finish_reason=FinishReason.TOOL_CALLS,
        model="deterministic",
        provider="mock",
    )
    provider = MockModelProvider([reply, "done"])
    tool = _echo()
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({"echo"}),
            guardrail_policy=GuardrailPolicy(policies=("decides",)),
            tool_policy=ToolPolicy(parallel_calls=True),
        ),
    )
    runtime.tools.register(tool)
    runtime.policies.register(Decides(PolicyAction.TRANSFORM, PolicyPoint.PLAN_TOOL))

    run = await runtime.run(_request())

    assert run.status == RunStatus.COMPLETED, run.error
    assert tool.calls == [{"key": CHANGED}, {"key": "other"}]  # type: ignore[attr-defined]
