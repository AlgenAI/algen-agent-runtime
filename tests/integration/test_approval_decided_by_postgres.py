"""Migration 020: who decided a run's approval, against a live PostgreSQL."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from algen_agent_runtime.approvals.service import ApprovalStatus, PostgresApprovalService
from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresRunStore
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="migration 020 requires a live PostgreSQL"
    ),
]

COLUMNS = {"decided_by": "text", "decided_at": "timestamp with time zone", "comment": "text"}


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def _run(db: PostgresDatabase, tenant: str) -> str:
    state = RunState(
        request=RunRequest(agent="test-agent", input="q", tenant_id=tenant, user_id="u"),
        status=RunStatus.AWAITING_APPROVAL,
    )
    await PostgresRunStore(db).create(state)
    return state.id


async def test_migration_020_adds_nullable_columns(database: PostgresDatabase) -> None:
    rows = await database.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name='algen_agent_runtime_approvals' AND column_name = ANY($1::text[])",
        list(COLUMNS),
    )
    assert {row["column_name"]: (row["data_type"], row["is_nullable"]) for row in rows} == {
        column: (kind, "YES") for column, kind in COLUMNS.items()
    }
    applied = await database.fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
    assert "020_approval_decided_by.sql" in {row["name"] for row in applied}


async def test_the_decision_is_stored_in_columns_and_read_back(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    service = PostgresApprovalService(database)
    run_id = await _run(database, tenant)
    created = await service.create(run_id, "s1", tenant, "Send", {"to": "bob"}, "external", 600)

    decided = await service.decide(
        created.id, tenant, ApprovalStatus.APPROVED, decided_by="carol", comment="Checked."
    )

    row = await database.fetchrow(
        "SELECT approval, decided_by, decided_at, comment FROM algen_agent_runtime_approvals "
        "WHERE id=$1",
        created.id,
    )
    raw = row["approval"]
    stored = json.loads(raw) if isinstance(raw, str) else dict(raw)
    # Earlier releases read the JSON with extra="forbid": the decision stays out of it.
    assert not {"decided_by", "decided_at", "comment"} & set(stored)
    assert (row["decided_by"], row["comment"]) == ("carol", "Checked.")
    assert row["decided_at"] == decided.decided_at

    read = await service.get(created.id, tenant)
    assert read is not None
    assert (read.status, read.decided_by, read.decided_at, read.comment) == (
        ApprovalStatus.APPROVED,
        "carol",
        decided.decided_at,
        "Checked.",
    )
    [listed] = await service.list(tenant, status=ApprovalStatus.APPROVED)
    assert (listed.id, listed.decided_by) == (created.id, "carol")
    # An expiry after the decision keeps who approved it.
    expired = await service.expire(created.id, tenant, reason="tool_changed")
    assert expired.decided_by == "carol"
    read = await service.get(created.id, tenant)
    assert read is not None and (read.status, read.decided_by) == (ApprovalStatus.EXPIRED, "carol")


async def test_a_row_decided_before_020_reads_with_no_approver(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    service = PostgresApprovalService(database)
    run_id = await _run(database, tenant)
    created = await service.create(run_id, "s1", tenant, "Send", {}, "write", 600)
    # As an earlier release decides it: the JSON changes, the 020 columns stay NULL.
    stored = created.model_dump(
        mode="json",
        include={
            "id",
            "run_id",
            "step_id",
            "tenant_id",
            "proposed_action",
            "side_effect_summary",
            "redacted_parameters",
            "risk",
            "expires_at",
            "modified_parameters",
        },
    )
    await database.execute(
        "UPDATE algen_agent_runtime_approvals SET approval=$1::jsonb WHERE id=$2",
        json.dumps({**stored, "status": "rejected"}),
        created.id,
    )

    read = await service.get(created.id, tenant)

    assert read is not None and read.status == ApprovalStatus.REJECTED
    assert (read.decided_by, read.decided_at, read.comment) == (None, None, None)
