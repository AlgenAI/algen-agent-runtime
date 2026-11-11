"""The executor takes a validated approval bound to the call it covers (#117)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.exceptions.errors import ApprovalMismatchError, PolicyDeniedError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    SideEffect,
    Tool,
    ToolApproval,
    ToolContext,
    ToolDefinition,
    canonical_digest,
    tool_fingerprint,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus


def _definition(**updates: Any) -> ToolDefinition:
    values: dict[str, Any] = {
        "name": "mail.send",
        "version": "1",
        "description": "Send an email",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
        "side_effect": SideEffect.EXTERNAL,
        "requires_approval": True,
    }
    values.update(updates)
    return ToolDefinition(**values)


def _context(key: str = "k1") -> ToolContext:
    return ToolContext(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-a",
        user_id="alice",
        permissions=frozenset(),
        idempotency_key=key,
    )


def _send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"sent": arguments.get("to")}


def _executor(tool: Tool) -> tuple[ToolExecutor, InMemoryToolExecutionStore]:
    registry = ToolRegistry()
    registry.register(tool)
    store = InMemoryToolExecutionStore()
    return ToolExecutor(registry, CompositePolicyEngine(), executions=store), store


def _approval(arguments: dict[str, Any], **updates: Any) -> ToolApproval:
    values: dict[str, Any] = {
        "approval_id": "approval-1",
        "decision": "approved",
        "tool": "mail.send",
        "tool_version": "1",
        "arguments_sha256": canonical_digest(arguments),
    }
    values.update(updates)
    return ToolApproval(**values)


async def test_an_approval_bound_to_the_call_runs_it() -> None:
    tool = Tool(_definition(), _send)
    executor, _ = _executor(tool)
    arguments = {"to": "bob"}

    result = await executor.execute(
        "mail.send",
        arguments,
        _context(),
        approval=_approval(arguments, tool_fingerprint=tool_fingerprint(tool)),
    )

    assert result.value == {"sent": "bob"}


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"tool": "mail.delete"}, "approval.tool_mismatch"),
        ({"tool_version": "2"}, "approval.tool_mismatch"),
        ({"arguments_sha256": canonical_digest({"to": "eve"})}, "approval."),
        ({"tool_fingerprint": "0" * 64}, "approval.tool_changed"),
    ],
)
async def test_an_approval_that_does_not_cover_the_call_is_refused_before_the_ledger(
    updates: dict[str, Any], code: str
) -> None:
    sent: list[Any] = []
    executor, store = _executor(Tool(_definition(), lambda a, c: sent.append(a) or {}))
    arguments = {"to": "bob"}

    with pytest.raises(ApprovalMismatchError) as raised:
        await executor.execute(
            "mail.send", arguments, _context(), approval=_approval(arguments, **updates)
        )

    assert raised.value.code.startswith(code)
    assert str(raised.value).startswith(raised.value.code)
    assert isinstance(raised.value, PolicyDeniedError)
    assert sent == [] and await store.get("k1", "tenant-a") is None


async def test_the_argument_digest_is_canonical() -> None:
    executor, _ = _executor(Tool(_definition(), _send))
    approval = _approval({"b": [1, {"y": 2, "x": 1}], "a": "z"})

    await executor.execute(
        "mail.send", {"a": "z", "b": [1, {"x": 1, "y": 2}]}, _context(), approval=approval
    )

    with pytest.raises(ApprovalMismatchError, match=r"^approval\.arguments_mismatch"):
        await executor.execute("mail.send", {"a": "z", "b": [1]}, _context("k2"), approval=approval)


def test_an_approval_takes_only_an_approving_decision() -> None:
    with pytest.raises(ValueError):
        _approval({}, decision="rejected")
    with pytest.raises(ValueError):
        _approval({}, arguments_sha256="not-a-digest")


async def test_an_approval_is_checked_even_for_a_tool_that_does_not_require_one() -> None:
    executor, _ = _executor(Tool(_definition(requires_approval=False), _send))

    with pytest.raises(ApprovalMismatchError, match=r"^approval\.arguments_mismatch"):
        await executor.execute("mail.send", {"to": "x"}, _context(), approval=_approval({}))


# Agent runs: resume() binds the decision to the call it approved.


def _runtime(arguments: dict[str, Any], seen: list[dict[str, Any]]) -> AgentRuntime:
    provider = MockModelProvider([tool_call_response("mail.send", arguments), "done"])
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"mail.send"})))

    def send(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        seen.append(args)
        return {"sent": True}

    runtime.tools.register(Tool(_definition(input_schema={"type": "object"}), send))
    return runtime


async def _paused(runtime: AgentRuntime) -> RunState:
    state = await runtime.start(
        RunRequest(agent="test-agent", input="send it", tenant_id="tenant", user_id="user")
    )
    for _ in range(400):
        state = await runtime.status(state.id, "tenant")
        if state.status == RunStatus.AWAITING_APPROVAL:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(f"run did not pause: {state.status}")


@pytest.mark.parametrize("decision", ["approved", "modified"])
async def test_resume_binds_the_approval_to_the_call_and_its_arguments(decision: str) -> None:
    seen: list[dict[str, Any]] = []
    runtime = _runtime({"to": "bob"}, seen)
    paused = await _paused(runtime)
    approval_id = paused.pause_payload["approval_id"]
    call_id = paused.pause_payload["tool_call_id"]
    modified = {"to": "carol"}

    payload: dict[str, Any] = {"decision": decision}
    if decision == "modified":
        payload["modified_parameters"] = modified
    resumed = await runtime.resume(paused.id, "tenant", payload)
    bound = resumed.tool_approvals[call_id]
    done = await runtime.wait(paused.id, "tenant")

    expected = modified if decision == "modified" else {"to": "bob"}
    assert done.status == RunStatus.COMPLETED, done.error
    assert seen == [expected]
    assert (bound.approval_id, bound.decision, bound.tool, bound.tool_version) == (
        approval_id,
        decision,
        "mail.send",
        "1",
    )
    assert bound.arguments_sha256 == canonical_digest(expected)
    await runtime.shutdown(grace_seconds=5)


async def test_an_approved_call_whose_arguments_changed_after_the_decision_is_refused() -> None:
    """Something rewrites the approved call between the decision and the run: the executor
    refuses it, because the approval is bound to the arguments that were approved."""
    seen: list[dict[str, Any]] = []
    runtime = _runtime({"to": "bob"}, seen)
    tool = runtime.tools.get("mail.send")
    state = RunState(
        request=RunRequest(agent="test-agent", input="q", tenant_id="tenant", user_id="u"),
        approved_tool_call_ids={"c1"},
        tool_approvals={
            "c1": ToolApproval.for_call("approval-x", "approved", "mail.send", "1", {"to": "bob"})
        },
    )

    with pytest.raises(ApprovalMismatchError, match=r"^approval\.arguments_mismatch"):
        await runtime.tool_executor.execute(
            "mail.send",
            {"to": "eve"},
            _context(),
            approval=runtime._call_approval(state, "c1", tool),
        )
    assert seen == []


def test_a_call_approved_before_0_2_is_bound_to_the_arguments_it_runs_with() -> None:
    runtime = make_runtime()
    tool = Tool(_definition(), _send)
    state = RunState(
        request=RunRequest(agent="test-agent", input="q", tenant_id="t", user_id="u"),
        approved_tool_call_ids={"c1"},
    )

    legacy = runtime._call_approval(state, "c1", tool)

    assert legacy is not None and legacy.tool_fingerprint is None
    assert legacy.approval_id == "tool_call:c1"
    assert runtime._call_approval(state, "c2", tool) is None


def test_run_state_leaves_out_empty_tool_approvals_for_earlier_releases() -> None:
    state = RunState(request=RunRequest(agent="a", input="q", tenant_id="t", user_id="u"))
    assert "tool_approvals" not in state.model_dump(mode="json")
    state.tool_approvals["c1"] = ToolApproval.for_call("a1", "approved", "mail.send", "1", {})
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored.tool_approvals == state.tool_approvals
