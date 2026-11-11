"""Session ownership in PostgreSQL: claims, expiry, the 019 backfill, the sweep and the Runtime."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from importlib import resources
from uuid import uuid4

import pytest
from conftest import make_runtime

from algen_agent_runtime.exceptions.errors import NotFoundError
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.postgres import (
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
)
from algen_agent_runtime.persistence.retention import PostgresRetentionSweeper, RetentionPolicy
from algen_agent_runtime.types.contracts import Message, Role, RunRequest, RunState, RunStatus
from algen_agent_runtime.types.interfaces import SessionOwnership

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="session owners require a live PostgreSQL"
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


def _tenant() -> str:
    return f"tenant-{uuid4()}"


async def _owner(db: PostgresDatabase, tenant: str, session: str) -> str | None:
    row = await db.fetchrow(
        "SELECT user_id FROM algen_agent_runtime_session_owners "
        "WHERE tenant_id=$1 AND session_id=$2",
        tenant,
        session,
    )
    return None if row is None else str(row["user_id"])


async def test_a_session_is_claimed_by_its_first_user(database: PostgresDatabase) -> None:
    store = PostgresMemoryStore(database, retention_seconds=3600)
    tenant = _tenant()

    assert isinstance(store, SessionOwnership)
    assert await store.claim_session(tenant, "s1", "alice")
    assert await store.claim_session(tenant, "s1", "alice")
    assert not await store.claim_session(tenant, "s1", "bob")
    assert await _owner(database, tenant, "s1") == "alice"
    assert await store.claim_session(_tenant(), "s1", "bob")  # another tenant's session


async def test_the_owners_claim_renews_it_and_an_expired_claim_is_free(
    database: PostgresDatabase,
) -> None:
    store = PostgresMemoryStore(database, retention_seconds=3600)
    tenant = _tenant()
    await store.claim_session(tenant, "s1", "alice")
    await database.execute(
        "UPDATE algen_agent_runtime_session_owners SET expires_at = now() + interval '1 second' "
        "WHERE tenant_id=$1",
        tenant,
    )
    assert await store.claim_session(tenant, "s1", "alice")
    row = await database.fetchrow(
        "SELECT expires_at > now() + interval '3000 seconds' AS renewed "
        "FROM algen_agent_runtime_session_owners WHERE tenant_id=$1",
        tenant,
    )
    assert row["renewed"]

    await database.execute(
        "UPDATE algen_agent_runtime_session_owners SET expires_at = now() - interval '1 second' "
        "WHERE tenant_id=$1",
        tenant,
    )
    assert await store.claim_session(tenant, "s1", "bob")
    assert await _owner(database, tenant, "s1") == "bob"
    assert not await store.claim_session(tenant, "s1", "alice")


async def test_a_never_expiring_claim_is_never_taken(database: PostgresDatabase) -> None:
    store = PostgresMemoryStore(database, retention_seconds=3600)
    tenant = _tenant()
    await database.execute(
        "INSERT INTO algen_agent_runtime_session_owners (tenant_id, session_id, user_id) "
        "VALUES ($1, 's1', 'alice')",
        tenant,
    )

    assert not await store.claim_session(tenant, "s1", "bob")


async def test_the_migration_backfills_owners_of_sessions_with_memory(
    database: PostgresDatabase,
) -> None:
    tenant = _tenant()
    runs = PostgresRunStore(database)
    memory = PostgresMemoryStore(database, retention_seconds=3600)
    for user, session in (("alice", "kept"), ("bob", "kept"), ("carol", "forgotten")):
        await runs.create(
            RunState(
                request=RunRequest(
                    agent="test-agent",
                    input="hi",
                    tenant_id=tenant,
                    user_id=user,
                    session_id=session,
                ),
                session_id=session,
            )
        )
    await memory.append(tenant, "kept", [Message.text(Role.USER, "alice's turn")])
    # Before the upgrade nothing was claimed.
    await database.execute(
        "DELETE FROM algen_agent_runtime_session_owners WHERE tenant_id=$1", tenant
    )

    migration = (
        resources.files("algen_agent_runtime.persistence")
        .joinpath("migrations/019_session_owners.sql")
        .read_text()
    )
    await database.execute(migration)

    # The earliest run's user owns a session that still has memory; one without memory has none.
    assert await _owner(database, tenant, "kept") == "alice"
    assert await _owner(database, tenant, "forgotten") is None
    assert not await memory.claim_session(tenant, "kept", "bob")


async def test_the_sweep_removes_expired_owners(database: PostgresDatabase) -> None:
    store = PostgresMemoryStore(database, retention_seconds=3600)
    tenant = _tenant()
    await store.claim_session(tenant, "old", "alice")
    await store.claim_session(tenant, "live", "alice")
    await database.execute(
        "UPDATE algen_agent_runtime_session_owners SET expires_at = now() - interval '1 second' "
        "WHERE tenant_id=$1 AND session_id='old'",
        tenant,
    )

    report = await PostgresRetentionSweeper(database).sweep(RetentionPolicy())

    assert report.deleted["session_owners"] >= 1
    assert await _owner(database, tenant, "old") is None
    assert await _owner(database, tenant, "live") == "alice"


async def test_the_runtime_refuses_another_users_session(database: PostgresDatabase) -> None:
    private_note = "alice-private-1e9b"
    provider = MockModelProvider([private_note, "alice again"])
    runtime = make_runtime(provider)
    memory = PostgresMemoryStore(database, retention_seconds=3600)
    runtime.memory = memory
    runtime.contexts.get("default")._memory = memory  # type: ignore[attr-defined]
    tenant = _tenant()

    def request(user: str, text: str) -> RunRequest:
        return RunRequest(
            agent="test-agent", input=text, tenant_id=tenant, user_id=user, session_id="shared"
        )

    first = await runtime.run(request("alice", f"remember {private_note}"))
    assert first.status == RunStatus.COMPLETED, first.error

    with pytest.raises(NotFoundError):
        await runtime.start(request("bob", "what did alice say?"))

    again = await runtime.run(request("alice", "and now?"))
    assert again.status == RunStatus.COMPLETED, again.error
    assert private_note in provider.requests[-1].model_dump_json()
