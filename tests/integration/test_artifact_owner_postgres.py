"""Artifact owners in PostgreSQL (migration 023): stored, listed per user, erased with the user."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from algen_agent_runtime.approvals.service import PostgresApprovalService
from algen_agent_runtime.events.bus import PostgresEventBus
from algen_agent_runtime.persistence import UserDataService
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.types.contracts import Artifact, ArtifactStatus

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


def _artifact(tenant: str, user: str | None, name: str) -> Artifact:
    return Artifact(
        tenant_id=tenant, user_id=user, name=name, media_type="text/plain", data=name.encode()
    )


async def test_the_owner_is_stored_and_a_users_artifacts_are_listed(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    store = PostgresArtifactStore(database)
    mine = _artifact(tenant, "alice", "mine.txt")
    theirs = _artifact(tenant, "bob", "theirs.txt")
    legacy = _artifact(tenant, None, "legacy.txt")
    for artifact in (mine, theirs, legacy):
        await store.put(artifact)

    described = await store.describe(mine.id, tenant)
    loaded = await store.get(mine.id, tenant)
    old = await store.describe(legacy.id, tenant)
    assert described is not None and described.user_id == "alice"
    assert loaded is not None and loaded.user_id == "alice"
    assert old is not None and old.user_id is None
    assert [item.id for item in await store.list_for_user(tenant, "alice")] == [mine.id]
    assert await store.list_for_user(tenant, "alice", status=ArtifactStatus.QUARANTINED) == ()
    assert await store.list_for_user(f"other-{tenant}", "alice") == ()
    assert {item.user_id for item in await store.list(tenant)} == {"alice", "bob", None}
    updated = await store.set_status(mine.id, tenant, ArtifactStatus.QUARANTINED)
    assert updated is not None and updated.user_id == "alice"


async def test_an_upload_never_attached_is_erased_with_its_owner(
    database: PostgresDatabase,
) -> None:
    tenant = f"tenant-{uuid4()}"
    stores = {
        "runs": PostgresRunStore(database),
        "memory": PostgresMemoryStore(database, retention_seconds=3600),
        "events": PostgresEventBus(database),
        "approvals": PostgresApprovalService(database),
        "tool_executions": PostgresToolExecutionStore(database),
        "artifacts": PostgresArtifactStore(database),
    }
    lonely = _artifact(tenant, "alice", "lonely.txt")
    kept = _artifact(tenant, "bob", "kept.txt")
    for artifact in (lonely, kept):
        await stores["artifacts"].put(artifact)
    service = UserDataService(**stores)

    exported = await service.export_user(tenant, "alice")
    report = await service.erase_user(tenant, "alice")

    assert [item["id"] for item in exported["artifacts"]] == [lonely.id]
    assert report.counts["artifacts"] == 1
    assert await stores["artifacts"].describe(lonely.id, tenant) is None
    assert await stores["artifacts"].describe(kept.id, tenant) is not None


async def test_migration_023_adds_the_owner_column_and_index(database: PostgresDatabase) -> None:
    column = await database.fetchrow(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'algen_agent_runtime_artifacts' "
        "AND column_name = 'user_id'"
    )
    assert column is not None and column["is_nullable"] == "YES"
    index = await database.fetchrow(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = 'algen_agent_runtime_artifacts_user_idx'"
    )
    assert index is not None and "(tenant_id, user_id, created_at DESC)" in index["indexdef"]
