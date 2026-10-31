"""What every policy boundary does when a policy raises or times out instead of deciding.

``closed`` (the default everywhere) refuses the value at that boundary; ``open`` skips the failed
policy and lets the others decide. Either way the error is counted, logged by type only and
audited with its fail mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.config.settings import AppSettings, PolicySettings
from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.policies.contracts import (
    POLICY_ERROR_REASON_CODE,
    PolicyAction,
    PolicyDecision,
    PolicyErrorHandling,
    PolicyFailMode,
    PolicyPoint,
)
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.testing import fake_tool_context
from algen_agent_runtime.types.contracts import GuardrailPolicy, RunRequest, RunStatus

SECRET_IN_ERROR = "the payload was api_key=do-not-log-me"


class Flaky:
    """Raises (or hangs) at the boundaries it applies to."""

    category = "test"
    enforcement_mode = "block"

    def __init__(self, *points: PolicyPoint, hang: bool = False, name: str = "flaky") -> None:
        self.name = name
        self.applies_to = frozenset(points)
        self.hang = hang

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if self.hang:
            await asyncio.sleep(10)
        raise RuntimeError(SECRET_IN_ERROR)


class Counter:
    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []

    def add(self, amount: int, attributes: dict[str, Any]) -> None:
        self.added.append(attributes)


class Audits:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def append(self, event: Any) -> None:
        self.events.append(event)


def _engine(
    *policies: Any, handling: PolicyErrorHandling | None = None, audits: Any = None
) -> tuple[CompositePolicyEngine, Counter]:
    engine = CompositePolicyEngine(policies, error_handling=handling, audits=audits)
    counter = Counter()
    engine._error_counter = counter
    return engine, counter


CONTEXT = {"tenant_id": "t1", "user_id": "u1", "run_id": "run-1"}
# Boundaries every text run crosses; the attachments boundary is crossed only with files.
RUN_POINTS = [point for point in PolicyPoint if point is not PolicyPoint.ATTACHMENTS]


@pytest.mark.parametrize("point", list(PolicyPoint))
async def test_a_raising_policy_fails_closed_by_default_at_every_boundary(
    point: PolicyPoint,
) -> None:
    audits = Audits()
    engine, counter = _engine(Flaky(point), audits=audits)

    decision = await engine.evaluate(point.value, "payload", CONTEXT)

    assert decision.action == PolicyAction.DENY
    assert decision.reason_code == POLICY_ERROR_REASON_CODE
    assert decision.audit_metadata["fail_mode"] == "closed"
    [error] = decision.audit_metadata["policy_errors"]
    assert error == {
        "boundary": point.value,
        "policy": "flaky",
        "error_type": "RuntimeError",
        "fail_mode": "closed",
        "timed_out": False,
    }
    assert counter.added == [
        {
            "boundary": point.value,
            "policy": "flaky",
            "fail_mode": "closed",
            "error_type": "RuntimeError",
        }
    ]
    [audit] = audits.events
    assert (audit.action, audit.outcome, audit.tenant_id, audit.resource_id) == (
        "policy.error",
        "fail_closed",
        "t1",
        "run-1",
    )
    # The error's text may quote the payload; only its type is kept.
    assert SECRET_IN_ERROR not in repr(decision) + repr(audit)


@pytest.mark.parametrize("point", list(PolicyPoint))
async def test_a_raising_policy_fails_open_when_configured_and_is_audited(
    point: PolicyPoint,
) -> None:
    audits = Audits()
    engine, counter = _engine(
        Flaky(point),
        handling=PolicyErrorHandling(on_error=PolicyFailMode.OPEN),
        audits=audits,
    )

    decision = await engine.evaluate(point.value, "payload", CONTEXT)

    assert decision.action == PolicyAction.ALLOW
    assert decision.audit_metadata["fail_mode"] == "open"
    assert decision.audit_metadata["policy_errors"][0]["policy"] == "flaky"
    assert counter.added[0]["fail_mode"] == "open"
    [audit] = audits.events
    assert (audit.action, audit.outcome) == ("policy.error", "fail_open")


async def test_failing_open_skips_only_the_failed_policy() -> None:
    from algen_agent_runtime.policies.engine import SecretRedactionPolicy

    engine, _ = _engine(
        Flaky(PolicyPoint.AFTER_TOOL),
        SecretRedactionPolicy(),
        handling=PolicyErrorHandling(on_error=PolicyFailMode.OPEN),
    )

    fake = "".join(("abcd", "efgh", "12345"))  # not a literal secret scanners flag
    decision = await engine.evaluate("after_tool", f"api_key={fake}", {})

    assert decision.action == PolicyAction.TRANSFORM
    assert "abcdefgh12345" not in decision.value
    assert decision.audit_metadata["policy_errors"][0]["policy"] == "flaky"


async def test_a_boundary_override_beats_the_global_mode() -> None:
    handling = PolicyErrorHandling(
        on_error=PolicyFailMode.OPEN,
        boundary_on_error={PolicyPoint.BEFORE_TOOL: PolicyFailMode.CLOSED},
    )
    engine, _ = _engine(Flaky(PolicyPoint.BEFORE_TOOL, PolicyPoint.AFTER_TOOL), handling=handling)

    assert (await engine.evaluate("before_tool", {}, {})).action == PolicyAction.DENY
    assert (await engine.evaluate("after_tool", {}, {})).action == PolicyAction.ALLOW


async def test_a_policy_past_its_timeout_is_a_policy_error() -> None:
    engine, counter = _engine(
        Flaky(PolicyPoint.INPUT, hang=True),
        handling=PolicyErrorHandling(timeout_seconds=0.01),
    )

    decision = await engine.evaluate("input", "hello", {})

    assert decision.action == PolicyAction.DENY
    assert decision.audit_metadata["policy_errors"][0]["timed_out"] is True
    assert counter.added[0]["error_type"] == "TimeoutError"


async def test_a_policy_raising_a_denial_has_decided_even_when_failing_open() -> None:
    class Refuses(Flaky):
        async def evaluate(
            self, point: str, payload: Any, context: Mapping[str, Any]
        ) -> PolicyDecision:
            raise PolicyDeniedError("no")

    engine, counter = _engine(
        Refuses(PolicyPoint.INPUT), handling=PolicyErrorHandling(on_error=PolicyFailMode.OPEN)
    )

    with pytest.raises(PolicyDeniedError):
        await engine.evaluate("input", "hello", {})
    assert counter.added == []


async def test_a_policy_returning_no_decision_is_a_policy_error() -> None:
    class Broken(Flaky):
        async def evaluate(self, point: str, payload: Any, context: Mapping[str, Any]) -> Any:
            return {"action": "allow"}

    engine, _ = _engine(Broken(PolicyPoint.INPUT))

    decision = await engine.evaluate("input", "hello", {})

    assert decision.reason_code == POLICY_ERROR_REASON_CODE
    assert decision.audit_metadata["policy_errors"][0]["error_type"] == "TypeError"


async def test_the_audit_reads_the_tool_context_at_tool_boundaries() -> None:
    audits = Audits()
    engine, _ = _engine(Flaky(PolicyPoint.BEFORE_TOOL), audits=audits)
    context = fake_tool_context(run_id="r", tenant_id="t2", user_id="u2")

    await engine.evaluate("before_tool", {}, {"tool": _lookup().definition, "context": context})

    [audit] = audits.events
    assert (audit.tenant_id, audit.actor_id, audit.resource_id) == ("t2", "u2", "r")
    assert audit.metadata["tool"] == "lookup"


async def test_a_later_denial_beats_an_earlier_approval_requirement() -> None:
    class Approval:
        name = "approval"
        category = "test"
        enforcement_mode = "block"
        applies_to = frozenset({PolicyPoint.PLAN_TOOL})

        async def evaluate(self, point: str, payload: Any, context: Any) -> PolicyDecision:
            return PolicyDecision(
                action=PolicyAction.REQUIRE_APPROVAL, reason_code="needs", reason="needs"
            )

    class Deny(Approval):
        name = "deny"

        async def evaluate(self, point: str, payload: Any, context: Any) -> PolicyDecision:
            return PolicyDecision(action=PolicyAction.DENY, reason_code="no", reason="no")

    approval_only, _ = _engine(Approval())
    both, _ = _engine(Approval(), Deny())

    assert (await approval_only.evaluate("plan_tool", {}, {})).action == (
        PolicyAction.REQUIRE_APPROVAL
    )
    assert (await both.evaluate("plan_tool", {}, {})).action == PolicyAction.DENY


def _lookup() -> Tool:
    calls: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"value": 42}

    tool = Tool(
        ToolDefinition(
            name="lookup",
            version="1",
            description="Look a value up.",
            side_effect=SideEffect.READ,
            input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
            output_schema={"type": "object"},
        ),
        handler,
    )
    tool.calls = calls  # type: ignore[attr-defined]
    return tool


def _runtime(point: PolicyPoint, mode: PolicyFailMode) -> tuple[AgentRuntime, Tool]:
    provider = MockModelProvider([tool_call_response("lookup", {"key": "a"}), "answer"])
    tool = _lookup()
    agent = make_agent(
        enabled_tools=frozenset({"lookup"}),
        guardrail_policy=GuardrailPolicy(policies=("flaky",)),
    )
    runtime = make_runtime(provider, agent)
    runtime.tools.register(tool)
    engine = CompositePolicyEngine(
        (Flaky(point),),
        error_handling=PolicyErrorHandling(boundary_on_error={point: mode}),
        audits=runtime.audits,
    )
    runtime.policies = engine
    runtime.tool_executor._policy_engine = engine
    return runtime, tool


_REQUEST = RunRequest(agent="test-agent", input="look it up", tenant_id="t", user_id="u")


@pytest.mark.parametrize("point", RUN_POINTS)
async def test_a_run_is_refused_when_a_boundary_fails_closed(point: PolicyPoint) -> None:
    runtime, tool = _runtime(point, PolicyFailMode.CLOSED)

    result = await runtime.run(_REQUEST)

    assert result.status == RunStatus.FAILED
    if point in {PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL}:
        assert tool.calls == []  # type: ignore[attr-defined]
    audits = await runtime.audits.list("t", result.run_id)
    assert ("policy.error", "fail_closed") in {(audit.action, audit.outcome) for audit in audits}
    if point in {PolicyPoint.BEFORE_TOOL, PolicyPoint.AFTER_TOOL}:
        return  # The tool executor's boundaries are audited, not announced as run events.
    events = await runtime.events.history(result.run_id)
    assert any(
        event.type == "policy.evaluated"
        and event.data["boundary"] == point.value
        and event.data.get("fail_mode") == "closed"
        for event in events
    )


@pytest.mark.parametrize("point", RUN_POINTS)
async def test_a_run_continues_with_an_audit_when_a_boundary_fails_open(
    point: PolicyPoint,
) -> None:
    runtime, tool = _runtime(point, PolicyFailMode.OPEN)

    result = await runtime.run(_REQUEST)

    assert result.status == RunStatus.COMPLETED and result.output == "answer"
    assert tool.calls == [{"key": "a"}]  # type: ignore[attr-defined]
    audits = await runtime.audits.list("t", result.run_id)
    errors = [audit for audit in audits if audit.action == "policy.error"]
    assert errors and {audit.outcome for audit in errors} == {"fail_open"}
    assert {audit.metadata["boundary"] for audit in errors} == {point.value}


def test_settings_build_the_engine_error_handling() -> None:
    settings = AppSettings(
        policies=PolicySettings(
            on_error=PolicyFailMode.OPEN,
            boundary_on_error={PolicyPoint.BEFORE_TOOL: PolicyFailMode.CLOSED},
            evaluation_timeout_seconds=2.5,
        )
    )

    engine = build_container(settings).runtime.policies

    assert engine.error_handling.mode("before_tool") == PolicyFailMode.CLOSED
    assert engine.error_handling.mode("after_tool") == PolicyFailMode.OPEN
    assert engine.error_handling.timeout_seconds == 2.5


def test_defaults_fail_closed_everywhere_without_a_timeout() -> None:
    handling = AppSettings().policies.error_handling()

    assert {handling.mode(point) for point in PolicyPoint} == {PolicyFailMode.CLOSED}
    assert handling.timeout_seconds is None
