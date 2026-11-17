"""An approved tool call counts as one step, as an unapproved one does (found by the long soak).

The approval pause counts the tool step, and after the decision the step is planned and counted
again before the call runs. Before the fix an approved call cost two steps of ``max_steps``, so a
run that fitted its budget without an approval failed with "execution_steps budget exhausted"
once its call needed one, after the call had run and the model had answered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import TERMINAL_STATUSES, RunRequest, RunStatus


def _tool(requires_approval: bool, seen: list[Any]) -> Tool:
    return Tool(
        ToolDefinition(
            name="crm.update",
            version="1",
            description="Update a record",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=SideEffect.WRITE,
            requires_approval=requires_approval,
        ),
        lambda args, ctx: seen.append(args) or {"ok": True},
    )


async def _run(max_steps: int, *, approval: bool) -> tuple[RunStatus, str | None, list[Any]]:
    seen: list[Any] = []
    provider = MockModelProvider([tool_call_response("crm.update", {"id": "r-1"}), "updated"])
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({"crm.update"}),
            max_steps=max_steps,
            approval_policy={"require_for_side_effects": approval},
        ),
    )
    runtime.tools.register(_tool(approval, seen))
    try:
        state = await runtime.start(
            RunRequest(agent="test-agent", input="update", tenant_id="t", user_id="u")
        )
        if approval:
            for _ in range(400):
                state = await runtime.status(state.id, "t")
                if state.status in {RunStatus.AWAITING_APPROVAL, *TERMINAL_STATUSES}:
                    break
                await asyncio.sleep(0.005)
            if state.status is RunStatus.AWAITING_APPROVAL:
                await runtime.resume(state.id, "t", {"decision": "approved"}, decided_by="u")
        done = await runtime.wait(state.id, "t")
        return done.status, done.error, seen
    finally:
        await runtime.shutdown(grace_seconds=5)


async def _smallest_budget() -> int:
    """The fewest steps the run needs without an approval."""
    for steps in range(1, 10):
        status, _, _ = await _run(steps, approval=False)
        if status is RunStatus.COMPLETED:
            return steps
    raise AssertionError("the run never completed")


@pytest.mark.asyncio
async def test_an_approved_call_fits_the_budget_an_unapproved_one_fits() -> None:
    steps = await _smallest_budget()

    status, error, seen = await _run(steps, approval=True)

    assert status is RunStatus.COMPLETED, error
    assert seen == [{"id": "r-1"}]


@pytest.mark.asyncio
async def test_the_budget_still_binds_with_an_approval() -> None:
    steps = await _smallest_budget()

    status, error, _ = await _run(steps - 1, approval=True)

    assert status is not RunStatus.COMPLETED
    assert error is not None and "budget" in error
