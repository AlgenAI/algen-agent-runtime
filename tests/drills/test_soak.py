"""A bounded soak drill: many runs, approvals and conversation turns at once on live PostgreSQL.

Off by default (marker ``soak``); set ``ALGEN_DRILL_SOAK_RUNS`` to run it:

    ALGEN_DRILL_SOAK_RUNS=200 scripts/integration-tests.sh tests/drills -m soak -s

``ALGEN_DRILL_SOAK_CONCURRENCY`` (default 20) sets how many are in flight at once and
``ALGEN_DRILL_SOAK_MAX_GROWTH_MB`` (default 64) the bound on traced Python memory growth after a
warm-up. It checks that every run ends completed with its effect exactly once, that the
connection pool stays within its maximum and is released, that no task is left behind and that
memory growth stays bounded, and prints the numbers.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import statistics
import time
import tracemalloc
from typing import Any

import pytest
from drill_support import (
    AGENT,
    APPROVAL_AGENT,
    TENANT,
    TOOL,
    USER,
    drill_settings,
    fresh_database,
    query,
    requires_postgres,
    running,
    server_connections,
    side_effects,
    until,
)

from algen_agent_runtime.conversations import MessageStatus
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

RUNS = int(os.environ.get("ALGEN_DRILL_SOAK_RUNS", "0") or 0)
CONCURRENCY = int(os.environ.get("ALGEN_DRILL_SOAK_CONCURRENCY", "20"))
MAX_GROWTH_MB = float(os.environ.get("ALGEN_DRILL_SOAK_MAX_GROWTH_MB", "64"))
POOL_MAX = 10

pytestmark = [
    pytest.mark.soak,
    pytest.mark.integration,
    pytest.mark.postgres,
    requires_postgres,
    pytest.mark.skipif(RUNS <= 0, reason="set ALGEN_DRILL_SOAK_RUNS to run the soak drill"),
]


async def _tool_run(process: Any, index: int) -> str:
    result = await process.runtime.run(
        RunRequest(agent=AGENT, input=f"soak {index}", tenant_id=TENANT, user_id=USER)
    )
    assert result.status is RunStatus.COMPLETED, result.error
    return str(result.run_id)


async def _approval_run(process: Any, index: int) -> str:
    runtime = process.runtime
    state = await runtime.start(
        RunRequest(agent=APPROVAL_AGENT, input=f"approve {index}", tenant_id=TENANT, user_id=USER)
    )

    async def waiting() -> bool:
        current = await runtime.status(state.id, TENANT)
        return current.status is RunStatus.AWAITING_APPROVAL

    await until(waiting, "the approval pause", within=120)
    await runtime.resume(state.id, TENANT, {"decision": "approved"})
    result = await runtime.wait(state.id, TENANT)
    assert result.status is RunStatus.COMPLETED, result.error
    return str(state.id)


async def _turn(process: Any, index: int) -> str:
    conversations = process.container.conversations
    conversation = await conversations.create(tenant_id=TENANT, user_id=USER, agent=AGENT)
    await conversations.submit(conversation.id, TENANT, USER, f"turn {index}")

    async def answered() -> Any:
        messages = await conversations.messages(conversation.id, TENANT)
        last = messages[-1] if messages else None
        done = {MessageStatus.COMPLETED, MessageStatus.FAILED}
        return last if last is not None and last.status in done else None

    message = await until(answered, "the conversation turn", within=120)
    assert message.status is MessageStatus.COMPLETED, message
    return str(conversation.id)


async def test_soak() -> None:
    settings = drill_settings(
        storage={"postgres_max_pool_size": POOL_MAX},
        # The provider's own rate limit (600 a minute by default) would pace the soak instead of
        # the Runtime and its stores.
        providers={
            "mock": {
                "type": "mock",
                "default_model": "deterministic",
                "requests_per_minute": 1_000_000,
            }
        },
    )
    kinds = (_tool_run, _tool_run, _tool_run, _approval_run, _turn)
    async with fresh_database("soak") as dsn:
        baseline_connections = await server_connections(dsn)
        tasks_before = asyncio.all_tasks()
        latencies: list[float] = []
        slowest: dict[str, float] = {}
        pool_peak = 0
        warmup = max(1, RUNS // 10)
        traced_start = 0
        async with running(dsn, settings=settings) as process:
            (database,) = [
                item
                for item in process.container.resources
                if type(item).__name__ == "PostgresDatabase"
            ]
            gate = asyncio.Semaphore(CONCURRENCY)
            finished = 0

            async def one(index: int) -> tuple[str, str]:
                nonlocal finished, pool_peak, traced_start
                async with gate:
                    work = kinds[index % len(kinds)]
                    started = time.monotonic()
                    ident = await work(process, index)
                    latencies.append(time.monotonic() - started)
                    slowest[work.__name__] = max(slowest.get(work.__name__, 0.0), latencies[-1])
                    pool = database._pool
                    if pool is not None:
                        pool_peak = max(pool_peak, pool.get_size())
                    finished += 1
                    if finished == warmup:
                        gc.collect()
                        tracemalloc.start()
                        traced_start = tracemalloc.get_traced_memory()[0]
                    return work.__name__, ident

            began = time.monotonic()
            results = await asyncio.gather(*(one(index) for index in range(RUNS)))
            elapsed = time.monotonic() - began
            gc.collect()
            traced_end = tracemalloc.get_traced_memory()[0] if tracemalloc.is_tracing() else 0
            tracemalloc.stop()
            growth_mb = max(0, traced_end - traced_start) / 1_000_000
            assert not process.runtime._tasks
            assert not process.runtime._outage_runs

        # Every run ended, every effect once.
        runs = [ident for kind, ident in results if kind != "_turn"]
        effects = await side_effects(dsn)
        assert len(effects) == len(set(effects)) == RUNS
        active = await query(
            dsn,
            "SELECT count(*) AS n FROM algen_agent_runtime_runs "
            "WHERE state->>'status' NOT IN ('completed', 'failed', 'cancelled', 'timed_out')",
        )
        assert active[0]["n"] == 0
        ledger = await query(
            dsn,
            "SELECT status, count(*) AS n FROM algen_agent_runtime_tool_executions "
            "WHERE tool_name=$1 GROUP BY status",
            TOOL,
        )
        assert {row["status"]: row["n"] for row in ledger} == {"completed": RUNS}
        # Resources: the pool stayed within its bound and was released; no task was left.
        assert pool_peak <= POOL_MAX
        for _ in range(100):
            if await server_connections(dsn) <= baseline_connections:
                break
            await asyncio.sleep(0.05)
        assert await server_connections(dsn) <= baseline_connections
        await asyncio.sleep(0)
        leaked = [task for task in asyncio.all_tasks() - tasks_before if not task.done()]
        assert leaked == [], leaked
        assert growth_mb < MAX_GROWTH_MB, growth_mb
        ordered = sorted(latencies)
        report = {
            "work_items": RUNS,
            "runs": len(runs),
            "turns": RUNS - len(runs),
            "concurrency": CONCURRENCY,
            "seconds": round(elapsed, 2),
            "per_second": round(RUNS / elapsed, 1),
            "latency_p50_s": round(statistics.median(ordered), 3),
            "latency_p95_s": round(ordered[int(len(ordered) * 0.95) - 1], 3),
            "latency_max_s": round(ordered[-1], 3),
            "slowest_by_kind_s": {kind: round(value, 3) for kind, value in slowest.items()},
            "pool_peak": pool_peak,
            "pool_max": POOL_MAX,
            "traced_growth_mb": round(growth_mb, 2),
        }
        print("SOAK " + json.dumps(report))
