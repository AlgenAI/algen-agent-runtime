"""Schema upgrade and rollback drills against each previous release (``PREVIOUS_RELEASES`` in
drill_support: ``0.1.0a23``, the previous minor line's last release, and ``0.2.0b3``, the release
before this one; ``ALGEN_DRILL_PREVIOUS_RELEASE`` runs one release instead).

Upgrade: a database a previous release migrated (through its last migration, ``016`` for
0.1.0a23 and ``022`` for 0.2.0b3) and wrote to is started by the current code, which applies the
new migrations (if any) and carries on the previous release's paused run. Rollback: after the
current code has migrated and written to a database, the previous release starts on it again (the
migrations are additive and stay in place) and reads and resumes what the current code wrote.

The previous releases' code comes from their git tags; without a tag (a shallow checkout) the
drills that run it skip, and the schema-only upgrade drill still runs.
"""

from __future__ import annotations

import pytest
from drill_support import (
    MIGRATIONS,
    PREVIOUS_RELEASES,
    TENANT,
    TOOL,
    Release,
    applied_migrations,
    apply_migrations,
    fresh_database,
    ledger,
    previous_release_migrations,
    previous_release_source,
    release_child,
    requires_postgres,
    running,
    side_effects,
)

from algen_agent_runtime.types.contracts import RunStatus

pytestmark = [pytest.mark.drill, pytest.mark.integration, pytest.mark.postgres, requires_postgres]

ALL_MIGRATIONS = sorted(path.name for path in MIGRATIONS.glob("*.sql"))


@pytest.fixture(params=PREVIOUS_RELEASES, ids=lambda release: release.tag)
def previous(request: pytest.FixtureRequest) -> Release:
    release: Release = request.param
    return release


def test_the_previous_release_matches_its_tag(previous: Release) -> None:
    """The release's last migration is the last one its tag ships, so the schema drill migrates
    exactly as that release did."""
    shipped = previous_release_migrations(previous.tag)
    assert shipped[-1].split("_")[0] == previous.last_migration
    assert shipped == [
        name for name in ALL_MIGRATIONS if name.split("_")[0] <= previous.last_migration
    ]


async def test_schema_upgrade_from_the_previous_release_migration_set(previous: Release) -> None:
    async with fresh_database() as dsn:
        applied = await apply_migrations(dsn, previous.last_migration)
        assert applied[-1].startswith(previous.last_migration)
        assert applied == ALL_MIGRATIONS[: len(applied)]
        async with running(dsn) as process:
            response = await process.client.get("/health/ready")
            assert response.status_code == 200, response.text
        assert await applied_migrations(dsn) == ALL_MIGRATIONS


async def test_upgrade_carries_on_what_the_previous_release_wrote(previous: Release) -> None:
    source = previous_release_source(previous.tag)
    last = previous.last_migration
    async with fresh_database() as dsn:
        written = await release_child(dsn, source, do="write")
        assert written["described"]["status"] == "awaiting_approval"
        assert max(await applied_migrations(dsn)).startswith(last)

        async with running(dsn) as process:
            runtime = process.runtime
            assert await applied_migrations(dsn) == ALL_MIGRATIONS
            completed = await runtime.status(written["completed"], TENANT)
            assert completed.status is RunStatus.COMPLETED
            approval = await runtime.approvals.get(written["approval_id"], TENANT)
            assert approval is not None
            # Columns added after the previous release are empty on what it wrote.
            if last < "015":
                assert approval.agent is None
            if last < "017":
                assert approval.tool_fingerprint is None and approval.rationale is None
            # The previous release's approvals are listed in the inbox too.
            listed = await runtime.approvals.list(TENANT, run_id=written["paused"])
            assert [item.id for item in listed] == [written["approval_id"]]
            await runtime.resume(written["paused"], TENANT, {"decision": "approved"})
            result = await runtime.wait(written["paused"], TENANT)
            assert result.status is RunStatus.COMPLETED, result.error
        assert await ledger(dsn, written["paused"]) == [(TOOL, "completed")]
        assert len(await side_effects(dsn, written["paused"])) == 1


async def test_rollback_to_the_previous_release_reads_and_resumes_current_data(
    previous: Release,
) -> None:
    source = previous_release_source(previous.tag)
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
