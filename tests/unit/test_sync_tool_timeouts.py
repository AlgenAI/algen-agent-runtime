"""Synchronous tools run in a worker thread: they meet their timeout and do not block the loop."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.authoring import read, tool
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionStatus,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry

TIMEOUT = 0.05
SLEEP = 0.4


def _context(key: str = "call-1") -> ToolContext:
    return ToolContext(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-a",
        user_id="alice",
        permissions=frozenset({"lookup.read", "double.read", "triple.read"}),
        idempotency_key=key,
    )


def _executor(registered: Tool) -> tuple[ToolExecutor, InMemoryToolExecutionStore]:
    registry = ToolRegistry()
    registry.register(registered)
    store = InMemoryToolExecutionStore()
    return ToolExecutor(registry, CompositePolicyEngine(), executions=store), store


def _sleeping_tool(side_effect: SideEffect, finished: list[str]) -> Tool:
    def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        time.sleep(SLEEP)
        finished.append("done")
        return {}

    return Tool(
        ToolDefinition(
            name="slow.call",
            version="1",
            description="Blocks",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=side_effect,
            idempotency=Idempotency.NON_IDEMPOTENT,
            timeout_seconds=TIMEOUT,
        ),
        handler,
    )


async def _ticks_while(call: Any) -> tuple[float, int, BaseException | None]:
    """How long ``call`` took, how often the loop ran meanwhile, and what it raised."""
    ticks = 0
    stop = asyncio.Event()

    async def tick() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.005)

    ticker = asyncio.create_task(tick())
    started = time.monotonic()
    error: BaseException | None = None
    try:
        await call
    except Exception as exc:
        error = exc
    elapsed = time.monotonic() - started
    stop.set()
    await ticker
    return elapsed, ticks, error


@pytest.mark.asyncio
async def test_a_synchronous_handler_times_out_on_schedule_without_blocking_the_loop() -> None:
    finished: list[str] = []
    executor, store = _executor(_sleeping_tool(SideEffect.EXTERNAL, finished))

    elapsed, ticks, error = await _ticks_while(executor.execute("slow.call", {}, _context()))

    assert isinstance(error, ToolExecutionError) and "timed out" in str(error)
    assert elapsed < SLEEP / 2
    assert ticks >= 3
    # The thread cannot be stopped, so a call that changes things has an unknown outcome.
    record = await store.get("call-1", "tenant-a")
    assert record is not None and record.status is ToolExecutionStatus.INDETERMINATE
    await asyncio.sleep(SLEEP)
    assert finished == ["done"]


@pytest.mark.asyncio
async def test_a_synchronous_tool_function_also_runs_off_the_event_loop() -> None:
    threads: list[int] = []
    finished: list[str] = []

    @tool(read(timeout=TIMEOUT))
    def lookup(sku: str) -> str:
        """Look up a SKU slowly."""
        threads.append(threading.get_ident())
        time.sleep(3 * SLEEP)
        finished.append(sku)
        return sku

    executor, _ = _executor(lookup)
    _, ticks, error = await _ticks_while(executor.execute("lookup", {"sku": "A1"}, _context()))

    # Every attempt (a read is retried) timed out while its function was still running.
    assert isinstance(error, ToolExecutionError) and "timed out" in str(error)
    assert finished == [] and ticks >= 3
    assert threads and threading.get_ident() not in threads


@pytest.mark.asyncio
async def test_fast_synchronous_and_asynchronous_handlers_still_return_their_results() -> None:
    @tool
    def double(value: int) -> int:
        """Double a number."""
        return value * 2

    @tool
    async def triple(value: int) -> int:
        """Triple a number."""
        return value * 3

    registry = ToolRegistry()
    registry.register(double)
    registry.register(triple)
    executor = ToolExecutor(registry, CompositePolicyEngine())

    assert (await executor.execute("double", {"value": 2}, _context("a"))).value == 4
    assert (await executor.execute("triple", {"value": 2}, _context("b"))).value == 6


def _counting() -> tuple[Any, dict[str, int], threading.Semaphore]:
    """A blocking job that counts its calls and its peak concurrency, and signals each finish."""
    lock = threading.Lock()
    done = threading.Semaphore(0)
    state = {"running": 0, "peak": 0, "calls": 0}

    def work() -> None:
        with lock:
            state["running"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["running"])
        time.sleep(SLEEP)
        with lock:
            state["running"] -= 1
        done.release()

    return work, state, done


async def _finished(done: threading.Semaphore, count: int) -> None:
    for _ in range(count):
        assert await asyncio.to_thread(done.acquire, timeout=5)


@pytest.mark.asyncio
async def test_timed_out_threads_keep_their_concurrency_slot_until_they_finish() -> None:
    work, state, done = _counting()
    registered = Tool(
        ToolDefinition(
            name="slow.call",
            version="1",
            description="Blocks",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=SideEffect.EXTERNAL,
            idempotency=Idempotency.NON_IDEMPOTENT,
            timeout_seconds=TIMEOUT,
            max_concurrency=1,
        ),
        lambda arguments, context: work() or {},
    )
    executor, _ = _executor(registered)

    results = await asyncio.gather(
        *(executor.execute("slow.call", {}, _context(f"call-{i}")) for i in range(3)),
        return_exceptions=True,
    )

    assert all(isinstance(r, ToolExecutionError) and "timed out" in str(r) for r in results)
    await _finished(done, 3)
    assert state["peak"] == 1


@pytest.mark.asyncio
async def test_a_timed_out_tool_function_is_not_retried_while_its_thread_still_runs() -> None:
    work, state, done = _counting()

    @tool(read(timeout=TIMEOUT))
    def lookup(sku: str) -> str:
        """Look up a SKU slowly."""
        work()
        return sku

    executor, _ = _executor(lookup)
    with pytest.raises(ToolExecutionError, match="timed out"):
        await executor.execute("lookup", {"sku": "A1"}, _context())
    await _finished(done, 1)
    # A read is retried on failure, but not over its own still-running attempt.
    assert state["calls"] == 1
