"""check_schema against a live PostgreSQL, and a host applying `migrations sql` itself."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from algen_agent_runtime.persistence import check_schema, migration_sql
from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresRunStore
from algen_agent_runtime.persistence.schema import expected_catalog, introspect
from algen_agent_runtime.types.contracts import RunRequest, RunState

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("POSTGRES_DSN"), reason="requires a live PostgreSQL"),
]


@pytest.fixture
async def scratch() -> AsyncIterator[str]:
    asyncpg = pytest.importorskip("asyncpg")
    schema = f"rt_check_{uuid4().hex[:10]}"
    yield schema
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await connection.close()


async def test_a_schema_the_runtime_migrated_has_no_issues(scratch: str) -> None:
    database = PostgresDatabase(os.environ["POSTGRES_DSN"], schema=scratch)
    await database.start()
    try:
        # The packaged catalog is what these migrations create (scripts/schema_catalog.py).
        actual = await introspect(database.fetch, scratch)
        assert actual["tables"] == expected_catalog()["tables"]
        report = await check_schema(database)
    finally:
        await database.close()
    assert report.ok, report.issues
    assert report.schema_name == scratch
    assert report.unrecorded_migrations == []
    report.raise_for_issues()


async def test_a_dropped_column_and_index_are_reported(scratch: str) -> None:
    database = PostgresDatabase(os.environ["POSTGRES_DSN"], schema=scratch)
    await database.start()
    try:
        await database.execute("ALTER TABLE algen_agent_runtime_approvals DROP COLUMN decided_by")
        await database.execute("DROP INDEX algen_agent_runtime_runs_user_idx")
        await database.execute(
            "DELETE FROM algen_agent_runtime_schema_migrations WHERE name LIKE '021%'"
        )
    finally:
        await database.close()

    report = await check_schema(os.environ["POSTGRES_DSN"], schema=scratch)

    assert {str(issue) for issue in report.issues} == {
        "missing_column algen_agent_runtime_approvals.decided_by: expected text, found None",
        "missing_index algen_agent_runtime_runs (algen_agent_runtime_runs_user_idx)",
    }
    assert report.unrecorded_migrations == ["021_user_data_indexes.sql"]
    with pytest.raises(AssertionError, match="decided_by"):
        report.raise_for_issues()


async def test_a_host_applying_migrations_sql_itself_gets_a_working_schema(scratch: str) -> None:
    asyncpg = pytest.importorskip("asyncpg")
    connection = await asyncpg.connect(os.environ["POSTGRES_DSN"])
    try:
        # As a host's migration tool would: up to 019 first, then the rest.
        everything = migration_sql(schema=scratch)
        up_to_019 = everything.split("-- algen-agent-runtime migration 020_")[0]
        await connection.execute(up_to_019)
        partial = await check_schema(connection, schema=scratch)
        assert {issue.kind for issue in partial.issues} >= {"missing_column"}
        assert any(issue.column == "decided_by" for issue in partial.issues)
        await connection.execute(migration_sql(since=19, schema=scratch))
        await connection.execute("RESET search_path")
        report = await check_schema(connection, schema=scratch)
    finally:
        await connection.close()
    assert report.ok, report.issues
    assert report.unrecorded_migrations == []

    # A Runtime that may not run DDL starts on it and works.
    database = PostgresDatabase(os.environ["POSTGRES_DSN"], schema=scratch, initialize_schema=False)
    try:
        store = PostgresRunStore(database)
        state = RunState(request=RunRequest(agent="a", input="x", tenant_id="t", user_id="u"))
        await store.create(state)
        assert await store.get(state.id, "t") is not None
    finally:
        await database.close()
