"""Noisy neighbour: tenant A floods the API while tenant B runs a steady stream.

One Runtime process on PostgreSQL and Redis (as in the load test). B creates one run every
``1 / ALGEN_PERF_B_RATE`` seconds (default 5 a second) for ``ALGEN_PERF_NOISY_SECONDS`` (default
20) and waits for each; its latency is from creation to completion. A runs
``ALGEN_PERF_A_CLIENTS`` (default 150) clients that create and wait for runs back to back and,
when refused, retry after 100 ms whatever ``Retry-After`` says. The model provider keeps its
default concurrency of 20 calls, shared by every tenant. Three phases, each on a fresh process:

    baseline     B alone
    unprotected  A floods; API admission limits off
    protected    A floods; per-tenant admission limits on (docs/operations.md, "API Admission
                 Rate Limiting and Concurrency Quotas"): run creation 600 a minute, burst 20,
                 at most 10 concurrent creation requests per tenant

It reports B's latency in each phase and A's accepted and refused requests, and checks that with
the limits on B has no errors and its p95 stays within ``max(2 x baseline p95, baseline p95 +
200 ms)``. The unprotected phase is reported, not bounded: it shows what the limits are for.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pytest
from perf_support import (
    AGENT,
    env_float,
    env_int,
    headers,
    perf_settings,
    plan,
    report,
    started,
    summary_ms,
)

from algen_agent_runtime.types.contracts import RunStatus

pytestmark = [
    pytest.mark.perf,
    pytest.mark.skipif(
        not (os.environ.get("POSTGRES_DSN") and os.environ.get("REDIS_URL")),
        reason="the noisy-neighbour test needs POSTGRES_DSN and REDIS_URL",
    ),
]

A, B = "perf-tenant-a", "perf-tenant-b"
B_RATE = env_float("ALGEN_PERF_B_RATE", 5)
SECONDS = env_float("ALGEN_PERF_NOISY_SECONDS", 20)
A_CLIENTS = env_int("ALGEN_PERF_A_CLIENTS", 150)
POOL_MAX = env_int("ALGEN_PERF_POOL_MAX", 20)
MODEL_DELAY = env_float("ALGEN_PERF_MODEL_DELAY_MS", 50) / 1000
LIMITS: dict[str, Any] = {
    "rate_limiting": {
        "enabled": True,
        "default_rate_per_minute": 6000,
        "default_burst": 200,
        "max_concurrent_run_requests_per_tenant": 10,
        "route_overrides": {"run_create": {"rate_per_minute": 600, "burst": 20}},
    }
}


async def _phase(dsn: str, *, flood: bool, limits: bool) -> dict[str, Any]:
    settings = perf_settings("postgres+redis", pool_max=POOL_MAX, api=LIMITS if limits else None)
    async with started(
        settings, dsn=dsn, redis_url=os.environ["REDIS_URL"], model_delay_seconds=MODEL_DELAY
    ) as process:
        runtime = process.runtime
        stop = asyncio.Event()
        b_latencies: list[float] = []
        b_errors: list[str] = []
        a_counts = {"accepted": 0, "refused": 0, "completed": 0, "errors": 0}

        async def create(tenant: str) -> Any:
            return await process.client.post(
                "/v1/runs", json={"agent": AGENT, "input": plan(1, 1)}, headers=headers(tenant)
            )

        async def flooder() -> None:
            while not stop.is_set():
                response = await create(A)
                if response.status_code == 429:
                    a_counts["refused"] += 1
                    await asyncio.sleep(0.1)
                    continue
                if response.status_code != 202:
                    a_counts["errors"] += 1
                    await asyncio.sleep(0.1)
                    continue
                a_counts["accepted"] += 1
                result = await runtime.wait(response.json()["run_id"], A)
                if result.status is RunStatus.COMPLETED:
                    a_counts["completed"] += 1

        async def one_b() -> None:
            began = time.perf_counter()
            response = await create(B)
            if response.status_code != 202:
                b_errors.append(f"{response.status_code}: {response.text[:200]}")
                return
            result = await runtime.wait(response.json()["run_id"], B)
            if result.status is not RunStatus.COMPLETED:
                b_errors.append(f"{result.status}: {result.error}")
                return
            b_latencies.append(time.perf_counter() - began)

        flooders = [asyncio.create_task(flooder()) for _ in range(A_CLIENTS)] if flood else []
        if flood:
            await asyncio.sleep(2)  # let the flood build up first
        b_runs: list[asyncio.Task[None]] = []
        began = time.perf_counter()
        for _ in range(int(SECONDS * B_RATE)):
            b_runs.append(asyncio.create_task(one_b()))
            await asyncio.sleep(1 / B_RATE)
        await asyncio.gather(*b_runs)
        b_elapsed = time.perf_counter() - began
        stop.set()
        await asyncio.gather(*flooders)
    return {
        "b_runs": len(b_runs),
        "b_completed": len(b_latencies),
        "b_errors": len(b_errors),
        "b_error_sample": b_errors[:3],
        "b_seconds": round(b_elapsed, 1),
        "b_latency": summary_ms(b_latencies),
        "a": dict(a_counts),
    }


async def test_noisy_neighbour() -> None:
    from drill_support import fresh_database

    async with fresh_database("perf") as dsn:
        baseline = await _phase(dsn, flood=False, limits=False)
        unprotected = await _phase(dsn, flood=True, limits=False)
        protected = await _phase(dsn, flood=True, limits=True)
    base_p95 = baseline["b_latency"]["p95_ms"]
    bound = max(2 * base_p95, base_p95 + 200)
    result = {
        "b_rate_per_second": B_RATE,
        "seconds": SECONDS,
        "a_clients": A_CLIENTS,
        "model_delay_ms": MODEL_DELAY * 1000,
        "provider_concurrency": 20,
        "limits": LIMITS["rate_limiting"],
        "baseline": baseline,
        "unprotected": unprotected,
        "protected": protected,
        "protected_bound_p95_ms": round(bound, 2),
    }
    report("noisy_neighbour", result)
    assert baseline["b_errors"] == 0, baseline
    assert protected["b_errors"] == 0, protected
    assert protected["a"]["refused"] > 0, protected
    assert protected["b_latency"]["p95_ms"] <= bound, result
