"""Benchmark baselines: the Runtime's latency overhead per step and per tool call.

The model answers at once and the tool does nothing, so a run's wall time is the Runtime's own
work. Runs go one at a time through ``runtime.run`` (in process, no HTTP), after a warm-up, on
in-memory stores and on PostgreSQL (every store), for four shapes:

    steps=0 tools=1   one model call, no tool (a direct answer)
    steps=1 tools=1   a tool step, then the answer (two model calls, one tool call)
    steps=5 tools=1   five tool steps, then the answer
    steps=1 tools=8   one step with eight parallel tool calls, then the answer

Per step = (steps=5 - steps=0) / 5, the cost of a model call plus one tool call and their
checkpoints. Per extra parallel tool call = (tools=8 - tools=1) / 7. Both from the p50s and from
the p95s. The direct cost of calling the model and the tool without the Runtime is measured too.

``ALGEN_PERF_ITERATIONS`` (default 300) sets the runs per shape and ``ALGEN_PERF_MAX_STEP_P95_MS``
(default 250) a loose regression bound on the per-step p95.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest
from perf_support import (
    AGENT,
    USER,
    env_float,
    env_int,
    noop_tool,
    percentile,
    perf_settings,
    plan,
    reply,
    report,
    started,
    summary_ms,
)

from algen_agent_runtime.types.contracts import (
    Message,
    ModelRequest,
    RunRequest,
    RunStatus,
)

pytestmark = [pytest.mark.perf]

ITERATIONS = env_int("ALGEN_PERF_ITERATIONS", 300)
WARMUP = max(10, ITERATIONS // 10)
MAX_STEP_P95_MS = env_float("ALGEN_PERF_MAX_STEP_P95_MS", 250)
SHAPES = ((0, 1), (1, 1), (5, 1), (1, 8))
TENANT = "perf-tenant"


async def _measure(process: Any, steps: int, tools: int) -> list[float]:
    runtime = process.runtime
    durations: list[float] = []
    for index in range(WARMUP + ITERATIONS):
        request = RunRequest(agent=AGENT, input=plan(steps, tools), tenant_id=TENANT, user_id=USER)
        began = time.perf_counter()
        result = await runtime.run(request)
        elapsed = time.perf_counter() - began
        assert result.status is RunStatus.COMPLETED, result.error
        if index >= WARMUP:
            durations.append(elapsed)
    return durations


async def _direct_costs() -> dict[str, float]:
    """The model and tool called directly, without the Runtime, for comparison."""
    from algen_agent_runtime.types.contracts import Role

    request = ModelRequest(
        messages=(Message.text(Role.USER, plan(0)),), model="deterministic", tools=()
    )
    model: list[float] = []
    for _ in range(ITERATIONS):
        began = time.perf_counter()
        reply(request)
        model.append(time.perf_counter() - began)
    tool = noop_tool()
    calls: list[float] = []
    for _ in range(ITERATIONS):
        began = time.perf_counter()
        await tool.handler({"step": 0, "index": 0}, None)  # type: ignore[arg-type]
        calls.append(time.perf_counter() - began)
    return {
        "model_call_p50_ms": round(percentile(model, 0.5) * 1000, 4),
        "tool_call_p50_ms": round(percentile(calls, 0.5) * 1000, 4),
    }


@pytest.mark.parametrize("stores", ["memory", "postgres"])
async def test_overhead_per_step_and_tool_call(stores: str) -> None:
    if stores == "postgres":
        if not os.environ.get("POSTGRES_DSN"):
            pytest.skip("the PostgreSQL baseline needs POSTGRES_DSN")
        from drill_support import fresh_database

        database = fresh_database("perf")
    else:
        database = None

    async def measure(dsn: str | None) -> dict[str, list[float]]:
        settings = perf_settings(stores)
        async with started(settings, dsn=dsn) as process:
            return {f"steps={s} tools={t}": await _measure(process, s, t) for s, t in SHAPES}

    if database is not None:
        async with database as dsn:
            samples = await measure(dsn)
    else:
        samples = await measure(None)

    shapes = {name: summary_ms(values) for name, values in samples.items()}
    base, five = samples["steps=0 tools=1"], samples["steps=5 tools=1"]
    one, eight = samples["steps=1 tools=1"], samples["steps=1 tools=8"]

    def derived(fraction: float) -> dict[str, float]:
        return {
            "per_step_ms": round(
                (percentile(five, fraction) - percentile(base, fraction)) / 5 * 1000, 2
            ),
            "per_parallel_tool_call_ms": round(
                (percentile(eight, fraction) - percentile(one, fraction)) / 7 * 1000, 2
            ),
        }

    result = {
        "stores": stores,
        "iterations": ITERATIONS,
        "warmup": WARMUP,
        "shapes": shapes,
        "p50": derived(0.5),
        "p95": derived(0.95),
        "direct": await _direct_costs(),
    }
    report("overhead", result)
    assert result["p95"]["per_step_ms"] < MAX_STEP_P95_MS, result
