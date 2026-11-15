"""Load test: N concurrent runs through the HTTP API on PostgreSQL and Redis.

Each run is created with ``POST /v1/runs`` (development headers, in process through
ASGITransport) and waited for with ``runtime.wait``; it makes a tool step and an answer: two model
calls with a simulated latency of ``ALGEN_PERF_MODEL_DELAY_MS`` (default 50) each and one no-op
tool call. Every store is on PostgreSQL but session memory, which is on Redis. ``concurrency``
clients each create and wait for runs back to back until ``runs`` have finished.

``ALGEN_PERF_LOAD_RUNS_PER_CLIENT`` (default 10) sets the runs per client and
``ALGEN_PERF_POOL_MAX`` (default 20) the PostgreSQL pool. It reports throughput, latency
percentiles from creation to completion, errors, the pool's peak and the process's peak RSS, and
fails on any error.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from perf_support import (
    AGENT,
    env_float,
    env_int,
    headers,
    peak_rss_mb,
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
        reason="the load test needs POSTGRES_DSN and REDIS_URL",
    ),
]

TENANT = "perf-load"
RUNS_PER_CLIENT = env_int("ALGEN_PERF_LOAD_RUNS_PER_CLIENT", 10)
POOL_MAX = env_int("ALGEN_PERF_POOL_MAX", 20)
MODEL_DELAY = env_float("ALGEN_PERF_MODEL_DELAY_MS", 50) / 1000


@pytest.mark.parametrize("concurrency", [50, 200])
async def test_concurrent_runs(concurrency: int) -> None:
    from drill_support import fresh_database

    total = concurrency * RUNS_PER_CLIENT
    settings = perf_settings("postgres+redis", pool_max=POOL_MAX, provider_concurrency=concurrency)
    rss_before = peak_rss_mb()
    async with fresh_database("perf") as dsn:
        async with started(
            settings, dsn=dsn, redis_url=os.environ["REDIS_URL"], model_delay_seconds=MODEL_DELAY
        ) as process:
            runtime = process.runtime
            latencies: list[float] = []
            errors: list[str] = []
            pool_peak = 0
            remaining = total

            async def client() -> None:
                nonlocal remaining, pool_peak
                while remaining > 0:
                    remaining -= 1
                    began = time.perf_counter()
                    response = await process.client.post(
                        "/v1/runs",
                        json={"agent": AGENT, "input": plan(1, 1)},
                        headers=headers(TENANT),
                    )
                    if response.status_code != 202:
                        errors.append(f"{response.status_code}: {response.text[:200]}")
                        continue
                    result = await runtime.wait(response.json()["run_id"], TENANT)
                    if result.status is not RunStatus.COMPLETED:
                        errors.append(f"{result.status}: {result.error}")
                        continue
                    latencies.append(time.perf_counter() - began)
                    pool = process.pool()
                    if pool is not None:
                        pool_peak = max(pool_peak, pool.get_size())

            began = time.perf_counter()
            await asyncio.gather(*(client() for _ in range(concurrency)))
            elapsed = time.perf_counter() - began
            assert not runtime._tasks

    result = {
        "stores": "postgres+redis",
        "concurrency": concurrency,
        "runs": total,
        "completed": len(latencies),
        "errors": len(errors),
        "seconds": round(elapsed, 2),
        "runs_per_second": round(len(latencies) / elapsed, 1),
        "model_delay_ms": MODEL_DELAY * 1000,
        "latency": summary_ms(latencies),
        "pool_peak": pool_peak,
        "pool_max": POOL_MAX,
        "peak_rss_mb_before": rss_before,
        "peak_rss_mb_after": peak_rss_mb(),
    }
    report("load", result)
    assert errors == [], errors[:5]
