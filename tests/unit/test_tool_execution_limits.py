"""Limits on the work synchronous tools leave running (#114).

Thread mode: the caller stops waiting on schedule; the work may continue. Every tool thread in
the process runs in one bounded pool with a bounded queue, and a full pool refuses calls before
they start. Process mode: a timed-out call's subprocess is killed, so the work really stops.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import process_tool_handlers as handlers
import pytest

from algen_agent_runtime.config.settings import AppSettings, RuntimeSettings, ToolThreadSettings
from algen_agent_runtime.exceptions.errors import (
    ToolCapacityError,
    ToolExecutionError,
    ToolNotAttemptedError,
)
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.blocking import (
    DEFAULT_MAX_QUEUED,
    DEFAULT_MAX_WORKERS,
    ToolThreadPool,
    configure_tool_threads,
    tool_thread_pool,
)
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
from algen_agent_runtime.tools.testing import fake_tool_context

# A fresh interpreter importing the runtime takes a while, more on a loaded CI machine.
PROCESS_TIMEOUT = 8.0
# The deadline of the call that hangs: long enough for its child to start.
HANG_TIMEOUT = 3.0


@pytest.fixture
def small_pool() -> Iterator[ToolThreadPool]:
    pool = configure_tool_threads(max_workers=1, max_queued=1)
    yield pool
    configure_tool_threads()


def _definition(name: str, **values: Any) -> ToolDefinition:
    values.setdefault("side_effect", SideEffect.EXTERNAL)
    values.setdefault("idempotency", Idempotency.NON_IDEMPOTENT)
    return ToolDefinition(
        name=name,
        version="1",
        description="test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        **values,
    )


def _executor(*tools: Tool) -> tuple[ToolExecutor, InMemoryToolExecutionStore]:
    registry = ToolRegistry()
    for item in tools:
        registry.register(item)
    store = InMemoryToolExecutionStore()
    return ToolExecutor(registry, CompositePolicyEngine(), executions=store), store


def _context(key: str, **values: Any) -> ToolContext:
    return fake_tool_context(tenant_id="t", idempotency_key=key, **values)


class _Gate:
    """Blocking handlers that wait until released, counting how many run at once."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started: list[str] = []

    def handler(self, label: str) -> Any:
        def run(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
                self.started.append(label)
            self.release.wait(5)
            with self.lock:
                self.running -= 1
            return {}

        return run


# Thread mode -------------------------------------------------------------------------------------


def test_the_default_pool_matches_asyncio_and_settings_size_it() -> None:
    assert ToolThreadSettings().max_workers is None
    assert ToolThreadSettings().max_queued == DEFAULT_MAX_QUEUED
    assert DEFAULT_MAX_WORKERS == min(32, (os.cpu_count() or 1) + 4)
    try:
        build_container(
            AppSettings(
                runtime=RuntimeSettings(
                    tool_threads=ToolThreadSettings(max_workers=3, max_queued=7)
                )
            )
        )
        pool = tool_thread_pool()
        assert (pool.max_workers, pool.max_queued) == (3, 7)
        # The same sizes keep the same pool.
        assert configure_tool_threads(3, 7) is pool
    finally:
        configure_tool_threads()


async def test_a_full_pool_refuses_a_call_before_it_starts(small_pool: ToolThreadPool) -> None:
    gate = _Gate()
    tools = [Tool(_definition(f"t{i}"), gate.handler(f"t{i}")) for i in range(3)]
    executor, store = _executor(*tools)

    running = asyncio.create_task(executor.execute("t0", {}, _context("k0")))
    queued = asyncio.create_task(executor.execute("t1", {}, _context("k1")))
    await asyncio.sleep(0.05)
    with pytest.raises(ToolCapacityError, match="tool thread pool is full") as refused:
        await executor.execute("t2", {}, _context("k2"))
    gate.release.set()
    await asyncio.gather(running, queued)

    assert refused.value.details() == {
        "code": "tool_capacity_exceeded",
        "max_workers": 1,
        "max_queued": 1,
    }
    # Different tools, one worker: never more than one ran at a time, and the refused one never.
    assert gate.peak == 1 and gate.started == ["t0", "t1"]
    record = await store.get("k2", "t")
    # Refused unstarted, so a call that writes is failed, not indeterminate.
    assert record is not None and record.status is ToolExecutionStatus.FAILED
    assert small_pool.pending == 0


async def test_a_queued_call_whose_caller_gave_up_never_starts(small_pool: ToolThreadPool) -> None:
    gate = _Gate()
    busy = Tool(_definition("busy"), gate.handler("busy"))
    late = Tool(_definition("late", timeout_seconds=0.05), gate.handler("late"))
    executor, store = _executor(busy, late)

    running = asyncio.create_task(executor.execute("busy", {}, _context("k0")))
    await asyncio.sleep(0.02)
    with pytest.raises(ToolNotAttemptedError, match="did not start"):
        await executor.execute("late", {}, _context("k1"))
    gate.release.set()
    await running
    await asyncio.sleep(0.05)

    assert gate.started == ["busy"]
    record = await store.get("k1", "t")
    assert record is not None and record.status is ToolExecutionStatus.FAILED
    assert small_pool.pending == 0


async def test_a_running_thread_keeps_its_worker_after_its_caller_times_out(
    small_pool: ToolThreadPool,
) -> None:
    gate = _Gate()
    slow = Tool(_definition("slow", timeout_seconds=0.05), gate.handler("slow"))
    executor, store = _executor(slow)

    with pytest.raises(ToolExecutionError, match="timed out"):
        await executor.execute("slow", {}, _context("k0"))

    # The caller stopped waiting on schedule; the work continues and still holds its worker.
    assert gate.started == ["slow"] and small_pool.pending == 1
    record = await store.get("k0", "t")
    assert record is not None and record.status is ToolExecutionStatus.INDETERMINATE
    gate.release.set()
    for _ in range(100):
        if small_pool.pending == 0:
            break
        await asyncio.sleep(0.01)
    assert small_pool.pending == 0


def test_pool_sizes_are_validated() -> None:
    with pytest.raises(ValueError):
        ToolThreadPool(0, 1)
    with pytest.raises(ValueError):
        ToolThreadPool(1, -1)


# Process mode ------------------------------------------------------------------------------------


def _process_tool(name: str, handler: Any, **values: Any) -> Tool:
    values.setdefault("timeout_seconds", PROCESS_TIMEOUT)
    values.setdefault("side_effect", SideEffect.READ)
    values.setdefault("idempotency", Idempotency.IDEMPOTENT)
    return Tool(_definition(name, execution="process", **values), handler)


async def test_a_process_tool_runs_in_another_process_and_returns_its_result() -> None:
    executor, _ = _executor(
        _process_tool("echo", handlers.echo), _process_tool("echo.async", handlers.echo_async)
    )

    result = await executor.execute(
        "echo", {"value": 7}, _context("k0", credentials={"authorization": "token-value"})
    )
    asynchronous = await executor.execute("echo.async", {"value": 8}, _context("k1"))

    assert result.value["pid"] != os.getpid() and result.value["value"] == 7
    # The caller's credentials reach the child explicitly; they never pickle with the context.
    assert result.value["credentials"] == {"authorization": "token-value"}
    assert asynchronous.value["pid"] != os.getpid() and asynchronous.value["value"] == 8


async def test_a_timed_out_process_tool_is_killed(tmp_path: Path) -> None:
    pid_file, done_file = tmp_path / "pid", tmp_path / "done"
    tool = _process_tool(
        "hang",
        handlers.hang,
        timeout_seconds=HANG_TIMEOUT,
        side_effect=SideEffect.EXTERNAL,
        idempotency=Idempotency.NON_IDEMPOTENT,
    )
    executor, store = _executor(tool)
    arguments = {"pid_file": str(pid_file), "done_file": str(done_file)}

    # The hang itself is 60 s.
    task = asyncio.create_task(executor.execute("hang", arguments, _context("k0")))
    for _ in range(int(HANG_TIMEOUT * 100)):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.01)
    started = time.monotonic()
    with pytest.raises(ToolExecutionError, match="timed out"):
        await task
    assert time.monotonic() - started < HANG_TIMEOUT + 1

    pid = int(pid_file.read_text())
    for _ in range(300):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("the timed-out tool's process is still running")
    assert not done_file.exists()
    # It may have sent its side effect before it was killed.
    record = await store.get("k0", "t")
    assert record is not None and record.status is ToolExecutionStatus.INDETERMINATE


async def test_a_process_tool_keeps_its_exceptions() -> None:
    executor, store = _executor(
        _process_tool("refuse", handlers.refuse, side_effect=SideEffect.EXTERNAL),
        _process_tool("fail", handlers.fail, retry_policy={"max_attempts": 1}),
    )

    with pytest.raises(ToolNotAttemptedError, match="nothing sent"):
        await executor.execute("refuse", {}, _context("k0"))
    with pytest.raises(ToolExecutionError, match="failed: ValueError"):
        await executor.execute("fail", {}, _context("k1"))

    record = await store.get("k0", "t")
    assert record is not None and record.status is ToolExecutionStatus.FAILED


def test_a_process_tool_needs_an_importable_handler() -> None:
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="top level of an importable module"):
        registry.register(_process_tool("closure", lambda arguments, context: {}))
    registry.register(_process_tool("echo", handlers.echo))
