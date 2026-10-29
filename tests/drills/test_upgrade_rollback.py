"""Schema upgrade and rollback drills against the previous release (``0.1.0a17``).

Upgrade: a database the previous release migrated (through ``014``) and wrote to is started by
the current code, which applies the new migrations and carries on the previous release's paused
run. Rollback: after the current code has migrated and written to a database, the previous
release starts on it again (the migrations are additive and stay in place) and reads and
resumes what the current code wrote.

The previous release's code comes from its git tag; without the tag (a shallow checkout) the
drills that run it skip, and the schema-only upgrade drill still runs.
"""

from __future__ import annotations

import pytest
from drill_support import (
    MIGRATIONS,
    TENANT,
    TOOL,
    applied_migrations,
    apply_migrations,
    fresh_database,
    ledger,
    previous_release_source,
    release_child,
    requires_postgres,
    running,
    side_effects,
)

from algen_agent_runtime.types.contracts import RunStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]

ALL_MIGRATIONS = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
# The last migration of the previous release.
PREVIOUS_LAST = "014"


async def test_schema_upgrade_from_the_previous_release_migration_set() -> None:
    async with fresh_database() as dsn:
        previous = await apply_migrations(dsn, PREVIOUS_LAST)
        assert previous[-1].startswith(PREVIOUS_LAST)
        assert len(previous) < len(ALL_MIGRATIONS)
        async with running(dsn) as process:
            response = await process.client.get("/health/ready")
            assert response.status_code == 200, response.text
        assert await applied_migrations(dsn) == ALL_MIGRATIONS


async def test_upgrade_carries_on_what_the_previous_release_wrote() -> None:
    source = previous_release_source()
    async with fresh_database() as dsn:
        written = await release_child(dsn, source, do="write")
        assert written["described"]["status"] == "awaiting_approval"
        assert max(await applied_migrations(dsn)).startswith(PREVIOUS_LAST)

        async with running(dsn) as process:
            runtime = process.runtime
            assert await applied_migrations(dsn) == ALL_MIGRATIONS
            completed = await runtime.status(written["completed"], TENANT)
            assert completed.status is RunStatus.COMPLETED
            approval = await runtime.approvals.get(written["approval_id"], TENANT)
            assert approval is not None and approval.agent is None  # written before 015
            # The previous release's approvals are listed in the inbox too.
            listed = await runtime.approvals.list(TENANT, run_id=written["paused"])
            assert [item.id for item in listed] == [written["approval_id"]]
            await runtime.resume(written["paused"], TENANT, {"decision": "approved"})
            result = await runtime.wait(written["paused"], TENANT)
            assert result.status is RunStatus.COMPLETED, result.error
        assert await ledger(dsn, written["paused"]) == [(TOOL, "completed")]
        assert len(await side_effects(dsn, written["paused"])) == 1


async def test_rollback_to_the_previous_release_reads_and_resumes_current_data() -> None:
    source = previous_release_source()
    async with fresh_database() as dsn:
        # The current code migrates the database and writes a completed and a paused run.
        written = await release_child(dsn, None, do="write")
        assert await applied_migrations(dsn) == ALL_MIGRATIONS
        assert written["described"]["approval"] == "pending"

        # The previous release starts on the migrated database and reads all of it.
        completed = await release_child(dsn, source, do="read", run_id=written["completed"])
        assert completed["described"]["status"] == "completed"
        assert completed["described"]["ledger"] == [[TOOL, "completed"]]
        paused = await release_child(
            dsn, source, do="read", run_id=written["paused"], approval_id=written["approval_id"]
        )
        assert paused["described"]["status"] == "awaiting_approval"
        assert paused["described"]["approval"] == "pending"

        # It resumes the paused run to completion, exactly once.
        resumed = await release_child(
            dsn, source, do="resume", run_id=written["paused"], approval_id=written["approval_id"]
        )
        assert resumed["described"]["status"] == "completed"
        assert resumed["described"]["approval"] == "approved"
        assert resumed["described"]["ledger"] == [[TOOL, "completed"]]
        assert len(await side_effects(dsn, written["paused"])) == 1

        # And rolling forward again reads what the previous release wrote.
        again = await release_child(
            dsn, None, do="read", run_id=written["paused"], approval_id=written["approval_id"]
        )
        assert again["described"]["status"] == "completed"
        assert again["described"]["events"] == resumed["described"]["events"]
