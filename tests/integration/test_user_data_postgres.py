"""Exporting and erasing one user's data in PostgreSQL (and session memory in Redis)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest
from user_data_seed import SEEDED_COUNTS, seed_user

from algen_agent_runtime.approvals.service import PostgresApprovalService
from algen_agent_runtime.conversations.feedback import PostgresConversationFeedbackStore
from algen_agent_runtime.conversations.stores import (
    PostgresConversationEventBus,
    PostgresConversationStore,
)
from algen_agent_runtime.events.bus import PostgresAuditLog, PostgresEventBus
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.memory.store import PostgresLearnedMemoryStore
from algen_agent_runtime.persistence import UserDataService
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import RunStatus
from algen_agent_runtime.workflows.store import PostgresWorkflowCheckpointStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("POSTGRES_DSN"), reason="requires a live PostgreSQL"),
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


def postgres_stores(db: PostgresDatabase) -> dict[str, Any]:
    return {
        "runs": PostgresRunStore(db),
        "memory": PostgresMemoryStore(db, retention_seconds=3600),
        "events": PostgresEventBus(db),
        "approvals": PostgresApprovalService(db),
        "tool_executions": PostgresToolExecutionStore(db),
        "artifacts": PostgresArtifactStore(db),
        "learned_memory": PostgresLearnedMemoryStore(db),
        "conversations": PostgresConversationStore(db),
        "conversation_events": PostgresConversationEventBus(db),
        "conversation_feedback": PostgresConversationFeedbackStore(db),
        "workflow_checkpoints": PostgresWorkflowCheckpointStore(db),
        "audits": PostgresAuditLog(db),
    }


async def count(db: PostgresDatabase, table: str, tenant: str) -> int:
    row = await db.fetchrow(
        f"SELECT count(*) AS n FROM algen_agent_runtime_{table} WHERE tenant_id=$1", tenant
    )
    return int(row["n"])


TABLES = (
    "runs",
    "events",
    "approvals",
    "tool_executions",
    "artifacts",
    "memory",
    "session_owners",
    "learned_memory",
    "memory_operations",
    "conversations",
    "conversation_messages",
    "conversation_events",
    "conversation_feedback",
    "workflow_checkpoints",
)


async def test_erasure_in_postgres_respects_foreign_keys_and_is_idempotent(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    stores = postgres_stores(database)
    alice = await seed_user(stores, tenant, "alice")
    await seed_user(stores, tenant, "bob")
    service = UserDataService(**stores)
    before = {table: await count(database, table, tenant) for table in TABLES}

    dry = await service.erase_user(tenant, "alice", dry_run=True)
    assert dry.counts == SEEDED_COUNTS and dry.blocked == []
    assert {table: await count(database, table, tenant) for table in TABLES} == before

    report = await service.erase_user(tenant, "alice")

    assert report.counts == SEEDED_COUNTS
    after = {table: await count(database, table, tenant) for table in TABLES}
    # Exactly Bob's rows are left: half of everything.
    assert after == {table: before[table] // 2 for table in TABLES}
    for run_id in alice.run_ids:
        assert await stores["runs"].get(run_id, tenant) is None
    assert await stores["memory"].session_owner(tenant, alice.session_id) is None
    assert (await service.erase_user(tenant, "alice")).total == 0
    audits = [e for e in await stores["audits"].list(tenant) if e.resource_id == "alice"]
    # Each erasure is audited, the second with nothing left to delete.
    assert [e.action for e in audits] == ["user_data.erased", "user_data.erased"]
    assert sorted(e.metadata["counts"]["runs"] for e in audits) == [0, 2]


async def test_export_in_postgres_is_json(database: PostgresDatabase) -> None:
    tenant = f"tenant-{uuid4()}"
    stores = postgres_stores(database)
    alice = await seed_user(stores, tenant, "alice")
    service = UserDataService(**stores)

    exported = await service.export_user(tenant, "alice", include_artifact_data=True)

    json.dumps(exported)
    assert [run["id"] for run in exported["runs"]] == alice.run_ids
    assert {item["id"] for item in exported["artifacts"]} == set(alice.artifact_ids)
    assert len(exported["conversations"][0]["messages"]) == 2
    assert exported["learned_memory"][0]["agent_id"] == "test-agent"


async def test_an_unresolved_tool_call_blocks_erasure_in_postgres(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    stores = postgres_stores(database)
    alice = await seed_user(stores, tenant, "alice")
    context = ToolContext(
        run_id=alice.run_ids[0],
        step_id="step-9",
        tenant_id=tenant,
        user_id="alice",
        permissions=frozenset(),
        idempotency_key=f"{tenant}:pending",
    )
    await stores["tool_executions"].begin(context, "send")
    service = UserDataService(**stores)

    with pytest.raises(ConflictError, match="started"):
        await service.erase_user(tenant, "alice")
    assert await count(database, "runs", tenant) == 2


async def test_unfinished_runs_block_erasure_in_postgres(database: PostgresDatabase) -> None:
    tenant = f"tenant-{uuid4()}"
    stores = postgres_stores(database)
    await seed_user(stores, tenant, "alice", status=RunStatus.AWAITING_CLARIFICATION)
    service = UserDataService(**stores)

    assert (await service.erase_user(tenant, "alice", dry_run=True)).blocked
    with pytest.raises(ConflictError):
        await service.erase_user(tenant, "alice")


async def test_the_run_store_removes_children_written_after_the_other_deletes(
    database: PostgresDatabase,
) -> None:
    # A child row that appears between the child deletes and the run delete must not break the
    # foreign key: delete_many removes it in the same statement.
    tenant = f"tenant-{uuid4()}"
    stores = postgres_stores(database)
    alice = await seed_user(stores, tenant, "alice")

    deleted = await stores["runs"].delete_many(tenant, alice.run_ids)

    assert deleted == {"runs": 2, "events": 2, "approvals": 2, "tool_executions": 2}
    assert await count(database, "artifacts", tenant) == 3  # run_id set NULL, not lost
