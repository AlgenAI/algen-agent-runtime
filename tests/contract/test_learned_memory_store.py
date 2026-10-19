from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.memory.contracts import LearnedMemory, MemoryMutation, MemoryScope
from algen_agent_runtime.memory.store import InMemoryLearnedMemoryStore, PostgresLearnedMemoryStore
from algen_agent_runtime.persistence.postgres import PostgresDatabase
from algen_agent_runtime.types.contracts import utc_now


@pytest.fixture(
    params=[
        "memory",
        pytest.param("postgres", marks=[pytest.mark.postgres, pytest.mark.integration]),
    ]
)
async def backend(request):
    scope = MemoryScope(tenant_id=str(uuid4()), user_id="user", agent_id="agent")
    if request.param == "memory":
        yield InMemoryLearnedMemoryStore(), scope
        return
    if not os.environ.get("POSTGRES_DSN"):
        pytest.skip("live PostgreSQL requires POSTGRES_DSN")
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield PostgresLearnedMemoryStore(db), scope
    finally:
        await db.execute(
            "DELETE FROM algen_agent_runtime_memory_operations WHERE tenant_id=$1", scope.tenant_id
        )
        await db.execute(
            "DELETE FROM algen_agent_runtime_learned_memory WHERE tenant_id=$1", scope.tenant_id
        )
        await db.close()


def mutation(
    scope, revision=0, *, key="report", content="Compare totals", deleted=False, operation=None
):
    return MemoryMutation(
        scope=scope,
        operation_id=operation or str(uuid4()),
        expected_revision=revision,
        record=LearnedMemory(
            key=key,
            revision=revision + 1,
            kind=None if deleted else "procedure",
            content=None if deleted else content,
            evidence=None if deleted else "User requested comparison",
            deleted=deleted,
            source_run_id="source-run",
            source_step_id="step",
            source_agent_version="1.0.0",
            updated_at=utc_now(),
            expires_at=None if deleted else utc_now() + timedelta(days=1),
        ),
    )


async def test_create_correct_delete_and_old_replay_cannot_resurrect(backend):
    store, scope = backend
    first = mutation(scope)
    receipt = await store.apply(first)
    assert receipt.revision == 1
    assert await store.apply(first) == receipt
    corrected = mutation(scope, 1, content="Use corrected totals")
    await store.apply(corrected)
    assert (await store.get(scope, "report")).content == "Use corrected totals"
    await store.apply(mutation(scope, 2, deleted=True))
    assert await store.apply(first) == receipt
    record = await store.get(scope, "report")
    assert record.deleted and record.content is None and record.evidence is None
    assert not await store.search(scope, "", 20)
    with pytest.raises(ConflictError):
        await store.apply(mutation(scope))
    await store.apply(mutation(scope, 3, content="Explicit new content"))
    assert (await store.get(scope, "report")).revision == 4


async def test_scope_isolation_and_literal_search(backend):
    store, scope = backend
    await store.apply(mutation(scope, content="100% verified _ totals"))
    assert len(await store.search(scope, "%", 20)) == 1
    assert not await store.search(scope, "nonexistent", 20)
    for field in ["tenant_id", "user_id", "agent_id"]:
        other = scope.model_copy(update={field: "other"})
        assert await store.get(other, "report") is None
        assert not await store.search(other, "", 20)


async def test_concurrent_creation_and_correction_use_compare_and_swap(backend):
    store, scope = backend
    for revision in [0, 1]:
        outcomes = await asyncio.gather(
            store.apply(mutation(scope, revision, content="First")),
            store.apply(mutation(scope, revision, content="Second")),
            return_exceptions=True,
        )
        assert len([r for r in outcomes if isinstance(r, ConflictError)]) == 1
        assert (await store.get(scope, "report")).revision == revision + 1


async def test_operation_identity_cannot_be_reused_for_different_content(backend):
    store, scope = backend
    first = mutation(scope, operation="fixed")
    await store.apply(first)
    with pytest.raises(ConflictError):
        await store.apply(mutation(scope, 1, operation="fixed", content="Changed"))
    assert (await store.get(scope, "report")).revision == 1


async def test_expiry_hides_search_and_purge_clears_payload_retaining_revision(backend):
    store, scope = backend
    item = mutation(scope)
    await store.apply(item)
    expired = item.record.model_copy(update={"expires_at": utc_now() - timedelta(seconds=1)})
    if isinstance(store, InMemoryLearnedMemoryStore):
        store._records[(scope.tenant_id, scope.user_id, scope.agent_id, "report")] = expired
    else:
        await store._database.execute(
            "UPDATE algen_agent_runtime_learned_memory SET record=$1::jsonb, expires_at=$2 WHERE tenant_id=$3",
            expired.model_dump_json(),
            expired.expires_at,
            scope.tenant_id,
        )
    assert not await store.search(scope, "", 20)
    assert await store.purge_expired() >= 1
    record = await store.get(scope, "report")
    assert record.content is None and record.evidence is None and record.revision == 1
    await store.apply(item)  # A replay cannot restore the purged payload.
    assert (await store.get(scope, "report")).content is None
    await store.apply(mutation(scope, 1))
    assert (await store.get(scope, "report")).revision == 2


async def test_reopening_postgres_adapter_preserves_records(backend):
    store, scope = backend
    await store.apply(mutation(scope))
    if isinstance(store, PostgresLearnedMemoryStore):
        await store._database.close()
        reopened = PostgresLearnedMemoryStore(store._database)
        assert (await reopened.get(scope, "report")).revision == 1


@pytest.mark.parametrize("serialized", [True, False])
def test_postgres_jsonb_decode_accepts_driver_strings_and_decoded_values(serialized):
    record = mutation(MemoryScope(tenant_id="t", user_id="u", agent_id="a")).record
    value = record.model_dump_json() if serialized else record.model_dump(mode="json")
    assert PostgresLearnedMemoryStore._decode(value) == record


async def test_postgres_unique_violation_is_normalized():
    class UniqueViolation(Exception):
        sqlstate = "23505"

    class Database:
        @asynccontextmanager
        async def transaction(self):
            raise UniqueViolation("driver details")
            yield

    with pytest.raises(ConflictError, match="concurrently modified"):
        await PostgresLearnedMemoryStore(Database()).apply(
            mutation(MemoryScope(tenant_id="t", user_id="u", agent_id="a"))
        )
