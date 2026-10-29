"""``storage.postgres_schema`` against a live PostgreSQL: migrations and stores in one schema."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from algen_agent_runtime.approvals.service import PostgresApprovalService
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresRunStore
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="schema placement requires a live PostgreSQL"
    ),
]


@pytest.fixture
async def schemas() -> AsyncIterator[list[str]]:
    pytest.importorskip("asyncpg")
    names = [f"algen_rt_{uuid4().hex[:12]}" for _ in range(2)]
    yield names
    admin = PostgresDatabase(os.environ["POSTGRES_DSN"], initialize_schema=False)
    try:
        for name in names:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    finally:
        await admin.close()


def _run(tenant: str) -> RunState:
    return RunState(
        request=RunRequest(agent="test-agent", input="q", tenant_id=tenant, user_id="u"),
        status=RunStatus.AWAITING_APPROVAL,
    )


async def test_migrations_and_stores_use_only_the_configured_schema(schemas: list[str]) -> None:
    first, second = schemas
    one = PostgresDatabase(os.environ["POSTGRES_DSN"], schema=first)
    two = PostgresDatabase(os.environ["POSTGRES_DSN"], schema=second)
    try:
        run = _run("tenant-a")
        await PostgresRunStore(one).create(run)
        approval = await PostgresApprovalService(one).create(
            run.id, "s1", "tenant-a", "Write", {}, "write", 600
        )

        tables = await one.fetch(
            "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = $1", first
        )
        names = {row["tablename"] for row in tables}
        assert {
            "algen_agent_runtime_schema_migrations",
            "algen_agent_runtime_runs",
            "algen_agent_runtime_approvals",
        } <= names
        applied = await one.fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
        assert "001_initial.sql" in {row["name"] for row in applied}

        # The second schema is migrated on its own and sees none of the first one's rows.
        assert await PostgresRunStore(two).get(run.id, "tenant-a") is None
        assert await PostgresApprovalService(two).get(approval.id, "tenant-a") is None
        stored = await PostgresRunStore(one).get(run.id, "tenant-a")
        assert stored is not None and stored.id == run.id

        # search_path is the session default: it survives the RESET ALL run on pool release.
        async with one.transaction() as connection:
            await connection.execute("RESET ALL")
            assert await connection.fetchval("SELECT current_schema()") == first
        row = await one.fetchrow("SELECT current_schema() AS name")
        assert row["name"] == first
    finally:
        await one.close()
        await two.close()

    # Starting again re-uses the schema and applies no migration twice.
    again = PostgresDatabase(os.environ["POSTGRES_DSN"], schema=first)
    try:
        rows = await again.fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
        assert len(rows) == len({row["name"] for row in rows})
        assert await PostgresRunStore(again).get(run.id, "tenant-a") is not None
    finally:
        await again.close()


async def test_a_missing_schema_is_refused_without_initialization(schemas: list[str]) -> None:
    database = PostgresDatabase(
        os.environ["POSTGRES_DSN"], schema=schemas[0], initialize_schema=False
    )
    with pytest.raises(ConfigurationError, match="does not exist"):
        await database.start()
    await database.close()


async def test_concurrent_first_starts_into_a_fresh_schema_all_succeed(schemas: list[str]) -> None:
    databases = [PostgresDatabase(os.environ["POSTGRES_DSN"], schema=schemas[0]) for _ in range(4)]
    try:
        # Without the migration lock, CREATE SCHEMA and the first migrations race and fail.
        await asyncio.gather(*(database.start() for database in databases))
        rows = await databases[0].fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
        assert len(rows) == len({row["name"] for row in rows})
    finally:
        for database in databases:
            await database.close()
