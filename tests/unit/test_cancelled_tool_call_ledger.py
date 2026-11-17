"""A cancelled tool call never leaves its ledger record ``started`` (0.2.0b5).

A ``started`` record waits for an operator to reconcile it, and retention keeps its run until
then, so one left by a cancellation lingers for good. The 0.2.0b5 six-hour soak found eight
after about 7,000 run cancellations: a call cancelled while its record was being written, while
it waited for a ``max_concurrency`` slot, or cancelled a second time while its failure was being
recorded (a turn and its run both cancelled).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import (
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

CONTEXT = ToolContext(
    run_id="run-1",
    step_id="step-1",
    tenant_id="tenant-a",
    user_id="alice",
    permissions=frozenset(),
    idempotency_key="k",
)


class _SlowStore(InMemoryToolExecutionStore):
    """Each write takes a few loop turns after it lands, as a database round trip does."""

    async def begin(self, context: ToolContext, tool_name: str) -> tuple[ToolExecutionRecord, bool]:
        reserved = await super().begin(context, tool_name)
        await asyncio.sleep(0.01)
        return reserved

    async def complete(self, idempotency_key: str, result: ToolResult) -> None:
        await asyncio.sleep(0.01)
        await super().complete(idempotency_key, result)

    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None:
        await asyncio.sleep(0.01)
        await super().fail(idempotency_key, error, indeterminate=indeterminate)


def _executor(
    side_effect: SideEffect, max_concurrency: int = 10
) -> tuple[ToolExecutor, _SlowStore]:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {}

    registry = ToolRegistry()
    registry.register(
        Tool(
            ToolDefinition(
                name="crm.call",
                version="1.0.0",
                description="A slow call",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=side_effect,
                max_concurrency=max_concurrency,
            ),
            handler,
        )
    )
    store = _SlowStore()
    return ToolExecutor(registry, CompositePolicyEngine(), executions=store), store


def _call(executor: ToolExecutor, key: str) -> asyncio.Task[ToolResult]:
    return asyncio.ensure_future(
        executor.execute("crm.call", {}, CONTEXT.model_copy(update={"idempotency_key": key}))
    )


async def _status(store: _SlowStore, key: str) -> ToolExecutionStatus:
    record = await store.get(key, CONTEXT.tenant_id)
    assert record is not None
    return record.status


@pytest.mark.parametrize("side_effect", [SideEffect.READ, SideEffect.WRITE])
async def test_cancelled_while_the_record_is_written(side_effect: SideEffect) -> None:
    executor, store = _executor(side_effect)
    call = _call(executor, "k1")
    await asyncio.sleep(0.005)  # begin has written the record and not yet returned
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    # The handler never ran: a definite failure, whatever the side effect.
    assert await _status(store, "k1") == ToolExecutionStatus.FAILED


@pytest.mark.parametrize("side_effect", [SideEffect.READ, SideEffect.WRITE])
async def test_cancelled_while_waiting_for_a_slot(side_effect: SideEffect) -> None:
    executor, store = _executor(side_effect, max_concurrency=1)
    holder = _call(executor, "holder")
    await asyncio.sleep(0.05)
    waiter = _call(executor, "waiter")
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await holder
    assert await _status(store, "waiter") == ToolExecutionStatus.FAILED
    assert await _status(store, "holder") == ToolExecutionStatus.COMPLETED


@pytest.mark.parametrize(
    ("side_effect", "outcome"),
    [
        (SideEffect.READ, ToolExecutionStatus.FAILED),
        (SideEffect.WRITE, ToolExecutionStatus.INDETERMINATE),
    ],
)
async def test_cancelled_again_while_the_failure_is_recorded(
    side_effect: SideEffect, outcome: ToolExecutionStatus
) -> None:
    executor, store = _executor(side_effect)
    call = _call(executor, "k1")
    await asyncio.sleep(0.05)  # the handler is running
    call.cancel()
    await asyncio.sleep(0.002)  # the failure is being written
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await _status(store, "k1") == outcome


async def test_cancelled_while_the_result_is_recorded() -> None:
    executor, store = _executor(SideEffect.WRITE)
    call = _call(executor, "k1")
    await asyncio.sleep(0.215)  # the handler returned; complete is being written
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await _status(store, "k1") == ToolExecutionStatus.COMPLETED
