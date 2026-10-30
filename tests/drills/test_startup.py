"""Startup drills: an empty database, a migrated one, and several processes starting at once."""

from __future__ import annotations

import asyncio

import pytest
from drill_support import (
    AGENT,
    HEADERS,
    MIGRATIONS,
    TENANT,
    USER,
    applied_migrations,
    fresh_database,
    query,
    requires_postgres,
    running,
    server_connections,
    side_effects,
)

from algen_agent_runtime.types.contracts import RunRequest, RunStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]

ALL_MIGRATIONS = sorted(path.name for path in MIGRATIONS.glob("*.sql"))


async def _serves(process: object) -> str:
    """The process is ready and runs a tool-calling run to completion; returns the run id."""
    response = await process.client.get("/health/ready")  # type: ignore[attr-defined]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ready"] is True and body["status"] == "ready", body
    assert body["dependencies"]["store.run"]["status"] == "up"
    result = await process.runtime.run(  # type: ignore[attr-defined]
        RunRequest(agent=AGENT, input="startup", tenant_id=TENANT, user_id=USER)
    )
    assert result.status is RunStatus.COMPLETED, result.error
    return result.run_id


async def test_first_start_on_an_empty_database_migrates_it_and_serves() -> None:
    async with fresh_database() as dsn:
        async with running(dsn) as process:
            await _serves(process)
        assert await applied_migrations(dsn) == ALL_MIGRATIONS


async def test_restart_on_a_migrated_database_keeps_its_data_and_applies_nothing() -> None:
    async with fresh_database() as dsn:
        async with running(dsn) as process:
            first = await _serves(process)
        before = await query(
            dsn, "SELECT name, applied_at FROM algen_agent_runtime_schema_migrations ORDER BY name"
        )
        async with running(dsn) as process:
            assert (await process.runtime.status(first, TENANT)).status is RunStatus.COMPLETED
            await _serves(process)
            response = await process.client.get(f"/v1/runs/{first}", headers=HEADERS)
            assert response.status_code == 200
        after = await query(
            dsn, "SELECT name, applied_at FROM algen_agent_runtime_schema_migrations ORDER BY name"
        )
        assert after == before


async def test_concurrent_first_starts_migrate_once_and_all_serve() -> None:
    """Four processes start together on one empty database: the migration lock lets one
    migrate while the others wait, then every one of them serves.

    The work starts once all four are up: a process that starts while another has a run in
    flight would recover that run as well (recovery is not coordinated between processes;
    single-node deployments run one process per database).
    """
    async with fresh_database() as dsn:
        baseline = await server_connections(dsn)
        contexts = [running(dsn) for _ in range(4)]
        processes = await asyncio.gather(*(context.__aenter__() for context in contexts))
        try:
            assert await applied_migrations(dsn) == ALL_MIGRATIONS
            runs = await asyncio.gather(*(_serves(process) for process in processes))
        finally:
            for context in contexts:
                await context.__aexit__(None, None, None)
        assert len(set(runs)) == len(processes)
        for run_id in runs:
            assert len(await side_effects(dsn, run_id)) == 1
        # Every pool closed again on stop.
        for _ in range(100):
            if await server_connections(dsn) <= baseline:
                break
            await asyncio.sleep(0.05)
        assert await server_connections(dsn) <= baseline
