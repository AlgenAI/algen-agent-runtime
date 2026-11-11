"""Migration 017: what a tool call's approval covers, against a live PostgreSQL."""

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
        not os.environ.get("POSTGRES_DSN"), reason="migration 017 requires a live PostgreSQL"
    ),
]

# The fields 0.1.0a17 accepts in the stored JSON (its ApprovalRequest has extra="forbid").
A17_FIELDS = {
    "id",
    "run_id",
    "step_id",
    "tenant_id",
    "proposed_action",
    "side_effect_summary",
    "redacted_parameters",
    "risk",
    "expires_at",
    "status",
    "modified_parameters",
}
COLUMNS = ("tool", "tool_version", "tool_fingerprint", "arguments_sha256", "status_reason")


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


async def _row(db: PostgresDatabase, approval_id: str) -> dict[str, object]:
    row = await db.fetchrow(
        f"SELECT approval, {', '.join(COLUMNS)} FROM algen_agent_runtime_approvals WHERE id=$1",
        approval_id,
    )
    raw = row["approval"]
    return {
        "json": dict(json.loads(raw) if isinstance(raw, str) else raw),
        **{column: row[column] for column in COLUMNS},
    }


async def test_migration_017_adds_nullable_text_columns(database: PostgresDatabase) -> None:
    rows = await database.fetch(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name='algen_agent_runtime_approvals' AND column_name = ANY($1::text[])",
        list(COLUMNS),
    )
    assert {row["column_name"]: (row["data_type"], row["is_nullable"]) for row in rows} == {
        column: ("text", "YES") for column in COLUMNS
    }
    applied = await database.fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
    assert "017_approval_tool_fingerprint.sql" in {row["name"] for row in applied}


async def test_the_fingerprint_and_expiry_reason_live_in_columns(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    service = PostgresApprovalService(database)
    run_id = await _run(database, tenant)
    fingerprint, digest = "a" * 64, "b" * 64

    created = await service.create(
        run_id,
        "s1",
        tenant,
        "Execute mail.send",
        {"to": "bob"},
        "external",
        600,
        agent="a",
        tool="mail.send",
        tool_version="1",
        tool_fingerprint=fingerprint,
        arguments_sha256=digest,
    )
    stored = await _row(database, created.id)
    assert set(stored["json"]) <= A17_FIELDS  # type: ignore[arg-type]
    assert (stored["tool"], stored["tool_version"], stored["tool_fingerprint"]) == (
        "mail.send",
        "1",
        fingerprint,
    )
    assert stored["arguments_sha256"] == digest and stored["status_reason"] is None

    await service.decide(created.id, tenant, ApprovalStatus.APPROVED)
    expired = await service.expire(created.id, tenant, reason="tool_changed")
    assert (expired.status, expired.status_reason) == (ApprovalStatus.EXPIRED, "tool_changed")
    stored = await _row(database, created.id)
    assert stored["status_reason"] == "tool_changed"
    assert set(stored["json"]) <= A17_FIELDS  # type: ignore[arg-type]

    read = await service.get(created.id, tenant)
    assert read is not None
    assert (read.status, read.status_reason, read.tool_fingerprint, read.arguments_sha256) == (
        ApprovalStatus.EXPIRED,
        "tool_changed",
        fingerprint,
        digest,
    )
    [listed] = await service.list(tenant, status=ApprovalStatus.EXPIRED)
    assert (listed.id, listed.status_reason) == (created.id, "tool_changed")
    # Expiring it again, or in another tenant, is refused.
    with pytest.raises(Exception, match="expired"):
        await service.expire(created.id, tenant, reason="tool_changed")
    assert await service.get(created.id, f"tenant-{uuid4()}") is None


async def test_a_row_written_before_017_reads_with_no_fingerprint(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    service = PostgresApprovalService(database)
    run_id = await _run(database, tenant)
    legacy = await service.create(run_id, "s1", tenant, "Old", {}, "write", 600, agent="a")
    await database.execute(
        f"UPDATE algen_agent_runtime_approvals SET {', '.join(f'{c}=NULL' for c in COLUMNS)} "
        "WHERE id=$1",
        legacy.id,
    )

    read = await service.get(legacy.id, tenant)
    assert read is not None
    assert all(getattr(read, column) is None for column in COLUMNS)
    decided = await service.decide(legacy.id, tenant, ApprovalStatus.APPROVED)
    assert decided.tool_fingerprint is None and decided.status == ApprovalStatus.APPROVED
