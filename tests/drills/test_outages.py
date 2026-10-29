"""Store outage and failover drills: PostgreSQL or Redis goes away under a running process.

The drills pause the store's container (an unresponsive server: connections hang) or restart it
(a failover as a single node sees one: every connection drops, the server comes back with what
it had committed). They need the container names ``scripts/integration-tests.sh`` exports and
skip without them.

What the Runtime promises (docs/operations.md#database-outages):

- readiness reports the store down within its probe timeout;
- calls fail with ``StoreUnavailableError`` (HTTP 503) within the configured timeouts, never
  with a driver exception and never by hanging;
- a run in flight keeps its last checkpoint; it is not failed, and it is driven again from that
  checkpoint once the store answers, its tool effects not repeated;
- the connection pool recovers without a process restart.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from drill_support import (
    AGENT,
    APPROVAL_AGENT,
    HEADERS,
    TENANT,
    TOOL,
    USER,
    Gate,
    drill_settings,
    fresh_database,
    ledger,
    postgres_container,
    query,
    redis_container,
    requires_postgres,
    requires_redis,
    running,
    side_effects,
    until,
    with_database,
)

from algen_agent_runtime.exceptions.errors import StoreUnavailableError
from algen_agent_runtime.types.contracts import RunRequest, RunStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration]

# Short bounds, so an outage is noticed within seconds.
TIMEOUTS = {
    "postgres_connect_timeout_seconds": 2,
    "postgres_command_timeout_seconds": 3,
    "redis_socket_timeout_seconds": 2,
}


def _settings(*, redis: bool = False) -> Any:
    extra: dict[str, Any] = {}
    if redis:
        extra["cache"] = {"backend": "redis", "redis_url": "env://DRILL_REDIS_URL"}
    return drill_settings(redis=redis, storage=TIMEOUTS, **extra)


async def _ready(process: Any) -> tuple[int, dict[str, Any]]:
    response = await process.client.get("/health/ready")
    return response.status_code, response.json()


async def _start_held(process: Any, gate: Gate, text: str) -> str:
    """A run whose tool has run and whose next model call is held at the gate."""
    state = await process.runtime.start(
        RunRequest(agent=AGENT, input=text, tenant_id=TENANT, user_id=USER)
    )
    await until(lambda: gate.entered >= 1, "the held run")
    return str(state.id)


async def _new_run_fails_cleanly(process: Any) -> None:
    started = time.monotonic()
    response = await process.client.post(
        "/v1/runs", json={"agent": AGENT, "input": "during the outage"}, headers=HEADERS
    )
    assert response.status_code == 503, response.text
    assert response.json()["kind"] == "unavailable"
    assert response.headers["retry-after"]
    with pytest.raises(StoreUnavailableError):
        await process.runtime.start(
            RunRequest(agent=AGENT, input="during the outage", tenant_id=TENANT, user_id=USER)
        )
    # Bounded by the connect and command timeouts (and a cancelled query's clean-up).
    assert time.monotonic() - started < 30


async def _retrying(call: Callable[[], Awaitable[Any]], within: float = 120.0) -> Any:
    """``call()``, retried while the store is unavailable, as a client retries a 503."""
    async with asyncio.timeout(within):
        while True:
            try:
                return await call()
            except StoreUnavailableError:
                await asyncio.sleep(0.5)


@requires_postgres
@pytest.mark.postgres
async def test_postgres_outage_is_reported_survived_and_recovered_without_restart() -> None:
    container = postgres_container()
    gate = Gate(after_tool=True)
    async with fresh_database() as dsn, running(dsn, gate=gate, settings=_settings()) as process:
        held = await _start_held(process, gate, "in flight during the outage")
        assert (await _ready(process))[0] == 200

        with container.paused():
            started = time.monotonic()
            status, body = await _ready(process)
            assert status == 503, body
            assert body["dependencies"]["store.run"]["status"] == "down"
            assert "store.run" in body["failing"]
            assert time.monotonic() - started < 15
            await _new_run_fails_cleanly(process)
            # The held run carries on into the outage: its checkpoint cannot be written, so
            # it is left as it was, not failed.
            gate.opened.set()
            await until(lambda: held in process.runtime._outage_runs, "the interrupted run")

        # Back without a restart: the pool reconnects and the interrupted run is driven again.
        result = await _retrying(lambda: process.runtime.wait(held, TENANT))
        assert result.status is RunStatus.COMPLETED, result.error
        assert len(await side_effects(dsn, held)) == 1
        assert await ledger(dsn, held) == [(TOOL, "completed")]
        await until(lambda: _ready(process), "readiness")
        assert (await _ready(process))[0] == 200
        done = await process.runtime.run(
            RunRequest(agent=AGENT, input="after the outage", tenant_id=TENANT, user_id=USER)
        )
        assert done.status is RunStatus.COMPLETED


@requires_postgres
@pytest.mark.postgres
async def test_a_process_started_while_postgres_is_down_fails_fast_and_starts_after() -> None:
    container = postgres_container()
    async with fresh_database() as dsn:
        with container.paused():
            started = time.monotonic()
            with pytest.raises(StoreUnavailableError):
                async with running(dsn, settings=_settings()):
                    pass
            assert time.monotonic() - started < 30
        async with running(dsn, settings=_settings()) as process:
            assert (await _ready(process))[0] == 200


@requires_postgres
@pytest.mark.postgres
async def test_failover_restart_drops_connections_and_loses_no_committed_state() -> None:
    """A primary failover, as one node sees it: the server restarts, every pooled connection
    drops, and the server comes back with what it had committed. (True HA failover, promoting
    a replica, is the database operator's concern; see docs/operations.md#failover.)"""
    container = postgres_container()
    gate = Gate(after_tool=True)
    async with fresh_database() as dsn, running(dsn, gate=gate, settings=_settings()) as process:
        runtime = process.runtime
        paused = await runtime.start(
            RunRequest(
                agent=APPROVAL_AGENT,
                input="paused over the failover",
                tenant_id=TENANT,
                user_id=USER,
            )
        )

        async def awaiting() -> bool:
            state = await runtime.status(paused.id, TENANT)
            return state.status is RunStatus.AWAITING_APPROVAL

        await until(awaiting, "the approval pause")
        held = await _start_held(process, gate, "in flight over the failover")
        committed = {
            table: (await query(dsn, f"SELECT count(*) AS n FROM {table}"))[0]["n"]
            for table in (
                "algen_agent_runtime_runs",
                "algen_agent_runtime_approvals",
                "algen_agent_runtime_events",
                "algen_agent_runtime_tool_executions",
            )
        }

        container.restart()
        # Every connection the pool held is gone; the held run's next write meets that.
        gate.opened.set()
        result = await _retrying(lambda: runtime.wait(held, TENANT))
        assert result.status is RunStatus.COMPLETED, result.error
        assert len(await side_effects(dsn, held)) == 1

        # Nothing committed before the restart was lost.
        for table, count in committed.items():
            now = (await query(dsn, f"SELECT count(*) AS n FROM {table}"))[0]["n"]
            assert now >= count, table
        state = await _retrying(lambda: runtime.status(paused.id, TENANT))
        assert state.status is RunStatus.AWAITING_APPROVAL
        approval = await runtime.approvals.get(state.pause_payload["approval_id"], TENANT)
        assert approval is not None and approval.status.value == "pending"
        await runtime.resume(paused.id, TENANT, {"decision": "approved"})
        resumed = await runtime.wait(paused.id, TENANT)
        assert resumed.status is RunStatus.COMPLETED, resumed.error
        assert len(await side_effects(dsn, paused.id)) == 1
        assert (await _ready(process))[0] == 200


def _redis_url() -> str:
    # A database index of the drills' own, so recovery never sees other tests' runs.
    return with_database(os.environ["REDIS_URL"], "7")


@requires_postgres
@requires_redis
@pytest.mark.postgres
@pytest.mark.redis
async def test_redis_outage_is_reported_survived_and_recovered_without_restart() -> None:
    pytest.importorskip("redis")
    from redis.asyncio import Redis

    container = redis_container()
    redis_url = _redis_url()
    client = Redis.from_url(redis_url)
    await client.flushdb()
    await client.aclose()
    gate = Gate(after_tool=True)
    async with (
        fresh_database() as dsn,
        running(dsn, gate=gate, redis_url=redis_url, settings=_settings(redis=True)) as process,
    ):
        held = await _start_held(process, gate, "in flight during the redis outage")
        status, body = await _ready(process)
        assert status == 200, body
        assert body["dependencies"]["store.run"]["backend"] == "redis"

        with container.paused():
            status, body = await _ready(process)
            assert status == 503, body
            assert body["dependencies"]["store.run"]["status"] == "down"
            # The cache is optional: it is reported down but does not fail readiness.
            assert body["dependencies"]["cache.redis"]["status"] == "down"
            assert "cache.redis" not in body["failing"]
            await _new_run_fails_cleanly(process)
            gate.opened.set()
            await until(lambda: held in process.runtime._outage_runs, "the interrupted run")

        result = await _retrying(lambda: process.runtime.wait(held, TENANT))
        assert result.status is RunStatus.COMPLETED, result.error
        assert len(await side_effects(dsn, held)) == 1
        assert (await _ready(process))[0] == 200
        done = await process.runtime.run(
            RunRequest(agent=AGENT, input="after the redis outage", tenant_id=TENANT, user_id=USER)
        )
        assert done.status is RunStatus.COMPLETED
