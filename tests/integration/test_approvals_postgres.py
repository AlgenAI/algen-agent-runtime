"""Listing approval requests against a live PostgreSQL."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import pytest

from algen_agent_runtime.approvals.service import ApprovalStatus, PostgresApprovalService
from algen_agent_runtime.persistence.postgres import PostgresDatabase, PostgresRunStore
from algen_agent_runtime.types.contracts import RunRequest, RunState, RunStatus, utc_now

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="approval listing SQL requires a live PostgreSQL"
    ),
]


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


async def test_postgres_approval_list_filters_pages_and_isolates_tenants(
    database: PostgresDatabase,
) -> None:
    tenant, other = f"tenant-{uuid4()}", f"tenant-{uuid4()}"
    service = PostgresApprovalService(database)
    run_a, run_b, run_other = (
        await _run(database, tenant),
        await _run(database, tenant),
        await _run(database, other),
    )
    first = await service.create(run_a, "s1", tenant, "Write", {"k": "v"}, "write", 600, agent="a")
    second = await service.create(run_a, "s2", tenant, "Send", {}, "external", 600, agent="a")
    third = await service.create(run_b, "s1", tenant, "Delete", {}, "high", 600, agent="b")
    lapsed = await service.create(run_b, "s2", tenant, "Old", {}, "write", 1, agent="b")
    await database.execute(
        "UPDATE algen_agent_runtime_approvals "
        "SET approval = jsonb_set(approval, '{expires_at}', to_jsonb($2::text)) WHERE id=$1",
        lapsed.id,
        (utc_now() - timedelta(minutes=1)).isoformat(),
    )
    await service.create(run_other, "s1", other, "Write", {}, "write", 600, agent="a")
    await service.decide(second.id, tenant, ApprovalStatus.APPROVED)

    listed = await service.list(tenant)
    assert {item.id for item in listed} == {first.id, second.id, third.id, lapsed.id}
    assert all(item.tenant_id == tenant for item in listed)
    assert [item.created_at for item in listed] == sorted(
        (item.created_at for item in listed), reverse=True
    )

    pending = await service.list(tenant, status=ApprovalStatus.PENDING)
    assert {item.id for item in pending} == {first.id, third.id}
    expired = await service.list(tenant, status=ApprovalStatus.EXPIRED)
    assert [(item.id, item.status) for item in expired] == [(lapsed.id, ApprovalStatus.EXPIRED)]
    approved = await service.list(tenant, status=ApprovalStatus.APPROVED)
    assert [item.id for item in approved] == [second.id]

    assert {item.id for item in await service.list(tenant, run_id=run_a)} == {first.id, second.id}
    assert {item.id for item in await service.list(tenant, agent="b")} == {third.id, lapsed.id}
    assert await service.list(tenant, run_id=run_other) == ()

    pages = [
        *(await service.list(tenant, limit=3)),
        *(await service.list(tenant, limit=3, offset=3)),
    ]
    assert [item.id for item in pages] == [item.id for item in listed]
    assert (await service.list(tenant, agent="a"))[-1].redacted_parameters == {"k": "v"}


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


async def _stored_json(db: PostgresDatabase, approval_id: str) -> dict[str, object]:
    row = await db.fetchrow(
        "SELECT approval FROM algen_agent_runtime_approvals WHERE id=$1", approval_id
    )
    raw = row["approval"]
    return dict(json.loads(raw) if isinstance(raw, str) else raw)


async def test_postgres_approval_agent_and_time_live_in_columns(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    service = PostgresApprovalService(database)
    run_id = await _run(database, tenant)

    created = await service.create(run_id, "s1", tenant, "Write", {}, "write", 600, agent="a")
    row = await database.fetchrow(
        "SELECT agent, created_at FROM algen_agent_runtime_approvals WHERE id=$1", created.id
    )
    assert (row["agent"], row["created_at"]) == ("a", created.created_at)
    assert set(await _stored_json(database, created.id)) <= A17_FIELDS
    read = await service.get(created.id, tenant)
    assert read is not None and (read.agent, read.created_at) == ("a", created.created_at)

    await service.decide(created.id, tenant, ApprovalStatus.MODIFIED, {"value": "x"})
    assert set(await _stored_json(database, created.id)) <= A17_FIELDS

    # A row written by 0.1.0a17: no agent column value, JSON in the a17 shape.
    legacy = await service.create(run_id, "s2", tenant, "Old", {}, "write", 600)
    await database.execute(
        "UPDATE algen_agent_runtime_approvals SET agent=NULL WHERE id=$1", legacy.id
    )
    [item] = [entry for entry in await service.list(tenant) if entry.id == legacy.id]
    assert item.agent is None and item.created_at is not None
    assert [entry.id for entry in await service.list(tenant, agent="a")] == [created.id]
