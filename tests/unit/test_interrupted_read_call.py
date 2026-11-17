"""A read-only call interrupted while running is run again on replay (found by the long soak).

A process killed during a tool call leaves its ledger record ``started``. A side-effecting call
then waits for its ``reconcile`` hook or an operator, as before. A call that changes nothing
(``side_effect`` ``none`` or ``read``) was refused the same way ("unfinished started execution;
manual reconciliation is required"), which failed a run recovered after the kill for a lookup,
and left the record ``started`` for good, so retention never deleted the run.
"""

from __future__ import annotations

from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import (
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionStatus,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

CONTEXT = ToolContext(
    run_id="run-1",
    step_id="step-1",
    tenant_id="tenant-a",
    user_id="alice",
    permissions=frozenset(),
    idempotency_key="run-1:call-1",
)


async def _interrupted(side_effect: SideEffect) -> tuple[ToolExecutor, Any, list[int]]:
    calls: list[int] = []

    async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(1)
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="crm.call",
                version="1",
                description="A call",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=side_effect,
            ),
            handler,
        )
    )
    store = InMemoryToolExecutionStore()
    # The process that reserved the call died before recording its outcome.
    _, reserved = await store.begin(CONTEXT, "crm.call")
    assert reserved
    return ToolExecutor(registry, CompositePolicyEngine(), executions=store), store, calls


@pytest.mark.parametrize("side_effect", [SideEffect.NONE, SideEffect.READ])
async def test_an_interrupted_read_only_call_runs_again(side_effect: SideEffect) -> None:
    executor, store, calls = await _interrupted(side_effect)

    result = await executor.execute("crm.call", {}, CONTEXT)

    assert result.value == {"ok": True}
    assert calls == [1]
    record = await store.get(CONTEXT.idempotency_key, CONTEXT.tenant_id)
    assert record is not None and record.status is ToolExecutionStatus.COMPLETED


@pytest.mark.parametrize("side_effect", [SideEffect.WRITE, SideEffect.EXTERNAL])
async def test_an_interrupted_side_effecting_call_still_waits(side_effect: SideEffect) -> None:
    executor, store, calls = await _interrupted(side_effect)

    with pytest.raises(ToolExecutionError, match="manual reconciliation is required"):
        await executor.execute("crm.call", {}, CONTEXT)

    assert calls == []
    record = await store.get(CONTEXT.idempotency_key, CONTEXT.tenant_id)
    assert record is not None and record.status is ToolExecutionStatus.STARTED
