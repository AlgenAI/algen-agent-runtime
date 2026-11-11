"""Semantic learned-memory recall on a live PostgreSQL (migration 018), with and without pgvector.

``POSTGRES_DSN`` (scripts/integration-tests.sh, postgres:17-alpine) has no vector extension, so
it exercises the portable SQL ranking. Set ``PGVECTOR_DSN`` to a database whose server has
pgvector (docker-compose.yml's pgvector/pgvector:pg17) to also run the pgvector ranking.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from uuid import uuid4

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.memory.contracts import (
    LearnedMemory,
    MemoryEmbedding,
    MemoryMutation,
    MemoryScope,
)
from algen_agent_runtime.memory.service import MEMORY_RECALL, MEMORY_WRITE
from algen_agent_runtime.memory.store import PostgresLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.persistence.postgres import PostgresDatabase
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    LearnedMemoryPolicy,
    RunRequest,
    RunState,
    RunStatus,
    utc_now,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

MODEL = '["embeddings","concepts",null]'


@pytest.fixture(
    params=[("POSTGRES_DSN", False), ("PGVECTOR_DSN", True), ("PGVECTOR_DSN", False)],
    ids=["portable", "pgvector", "pgvector-server-portable-sql"],
)
async def database(request) -> AsyncIterator[tuple[PostgresDatabase, bool]]:
    variable, use_pgvector = request.param
    if not os.environ.get(variable):
        pytest.skip(f"requires {variable}")
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ[variable])
    await db.start()
    try:
        if variable == "PGVECTOR_DSN":
            await db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        yield db, use_pgvector
    finally:
        await db.close()


@pytest.fixture
async def store(database) -> AsyncIterator[tuple[PostgresLearnedMemoryStore, MemoryScope]]:
    db, use_pgvector = database
    store = PostgresLearnedMemoryStore(db)
    if use_pgvector:
        assert "<#>" in await store._score_expression()
    # The portable SQL also runs on a server that has pgvector.
    store._pgvector = use_pgvector
    scope = MemoryScope(tenant_id=str(uuid4()), user_id="user", agent_id="agent")
    try:
        yield store, scope
    finally:
        await db.execute(
            "DELETE FROM algen_agent_runtime_memory_operations WHERE tenant_id=$1", scope.tenant_id
        )
        await db.execute(
            "DELETE FROM algen_agent_runtime_learned_memory WHERE tenant_id=$1", scope.tenant_id
        )


def embedding(*values: float, model: str = MODEL) -> MemoryEmbedding:
    return MemoryEmbedding.normalized(model, values)


def mutation(scope, key, revision=0, content="content", deleted=False, expires=timedelta(days=1)):
    return MemoryMutation(
        scope=scope,
        operation_id=str(uuid4()),
        expected_revision=revision,
        record=LearnedMemory(
            key=key,
            revision=revision + 1,
            kind=None if deleted else "fact",
            content=None if deleted else content,
            evidence=None if deleted else "evidence",
            deleted=deleted,
            source_run_id="run",
            source_step_id="step",
            source_agent_version="1.0.0",
            updated_at=utc_now(),
            expires_at=None if deleted else utc_now() + expires,
        ),
    )


async def test_migration_creates_the_vector_table_without_an_extension(database):
    db, _ = database
    row = await db.fetchrow(
        "SELECT to_regclass('algen_agent_runtime_learned_memory_vectors') IS NOT NULL AS present, "
        "EXISTS (SELECT 1 FROM algen_agent_runtime_schema_migrations "
        "WHERE name = '018_learned_memory_vectors.sql') AS applied"
    )
    assert row["present"] and row["applied"]


async def test_rank_orders_by_similarity_within_one_scope(store):
    store, scope = store
    await store.apply_embedded(mutation(scope, "billing"), embedding(1, 0, 0))
    await store.apply_embedded(mutation(scope, "weather"), embedding(0, 1, 0))
    await store.apply_embedded(mutation(scope, "mixed"), embedding(1, 1, 0))
    ranked = await store.rank(scope, embedding(1, 0.1, 0), 21)
    assert [record.key for record, _ in ranked] == ["billing", "mixed", "weather"]
    assert ranked[0][1] == pytest.approx(0.995, abs=0.01)
    assert 0 <= ranked[-1][1] < 0.2
    assert [r.key for r, _ in await store.rank(scope, embedding(1, 0, 0), 1)] == ["billing"]
    # Vectors of another model or size are never compared.
    assert await store.rank(scope, embedding(1, 0, 0, model="other"), 21) == ()
    assert await store.rank(scope, embedding(1, 0, 0, 0), 21) == ()
    for field in ["tenant_id", "user_id", "agent_id"]:
        other = scope.model_copy(update={field: "other"})
        assert await store.rank(other, embedding(1, 0, 0), 21) == ()
        assert await store.unembedded(other, MODEL, 21) == ()


async def test_corrections_deletion_and_expiry_remove_old_vectors(store):
    store, scope = store
    await store.apply_embedded(mutation(scope, "note"), embedding(1, 0))
    await store.apply_embedded(mutation(scope, "note", 1, "corrected"), embedding(0, 1))
    ranked = await store.rank(scope, embedding(0, 1), 21)
    assert [(r.key, r.revision) for r, _ in ranked] == [("note", 2)]
    assert ranked[0][1] == pytest.approx(1.0, abs=1e-5)
    # A plain apply (substring mode) drops the vector instead of leaving a stale one.
    await store.apply(mutation(scope, "note", 2, "plain"))
    assert await store.rank(scope, embedding(0, 1), 21) == ()
    assert [r.key for r in await store.unembedded(scope, MODEL, 21)] == ["note"]
    assert await store.put_embedding(scope, "note", 3, embedding(0, 1))
    assert not await store.put_embedding(scope, "note", 2, embedding(1, 0))
    await store.apply(mutation(scope, "note", 3, deleted=True))
    assert not await store.put_embedding(scope, "note", 4, embedding(1, 0))
    count = await store._database.fetchrow(
        "SELECT count(*) AS n FROM algen_agent_runtime_learned_memory_vectors WHERE tenant_id=$1",
        scope.tenant_id,
    )
    assert count["n"] == 0
    await store.apply_embedded(mutation(scope, "brief"), embedding(1, 0))
    await store._database.execute(
        "UPDATE algen_agent_runtime_learned_memory SET expires_at = now() - interval '1 second' "
        "WHERE tenant_id=$1",
        scope.tenant_id,
    )
    assert await store.rank(scope, embedding(1, 0), 21) == ()
    assert await store.purge_expired(10000) >= 1
    count = await store._database.fetchrow(
        "SELECT count(*) AS n FROM algen_agent_runtime_learned_memory_vectors WHERE tenant_id=$1",
        scope.tenant_id,
    )
    assert count["n"] == 0


async def test_purge_removes_vectors_an_older_release_left_behind(store):
    store, scope = store
    await store.apply_embedded(mutation(scope, "deleted"), embedding(1, 0))
    await store.apply_embedded(mutation(scope, "corrected"), embedding(1, 0))
    await store.apply_embedded(mutation(scope, "kept"), embedding(1, 0))
    # A release without vector handling deletes and corrects records but not their vectors.
    db = store._database
    await db.execute(
        "UPDATE algen_agent_runtime_learned_memory SET deleted=true, revision=2, "
        "record=jsonb_set(record, '{content}', 'null'::jsonb) WHERE tenant_id=$1 AND key='deleted'",
        scope.tenant_id,
    )
    await db.execute(
        "UPDATE algen_agent_runtime_learned_memory SET revision=2 "
        "WHERE tenant_id=$1 AND key='corrected'",
        scope.tenant_id,
    )
    await store.purge_expired(10000)
    rows = await db.fetch(
        "SELECT key FROM algen_agent_runtime_learned_memory_vectors WHERE tenant_id=$1",
        scope.tenant_id,
    )
    assert [row["key"] for row in rows] == ["kept"]


async def test_unembedded_lists_records_of_another_model(store):
    store, scope = store
    await store.apply(mutation(scope, "plain"))
    await store.apply_embedded(mutation(scope, "old"), embedding(1, 0, model="old"))
    await store.apply_embedded(mutation(scope, "current"), embedding(1, 0))
    assert {r.key for r in await store.unembedded(scope, MODEL, 21)} == {"plain", "old"}
    assert await store.put_embedding(scope, "old", 1, embedding(0, 1))
    assert {r.key for r in await store.unembedded(scope, MODEL, 21)} == {"plain"}


class Embeddings(MockModelProvider):
    provider_id = "embeddings"

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        words = ("invoice", "billing", "statement", "layout", "format")
        return [
            [float(any(w in text.lower() for w in words)), 0.1, float("rain" in text.lower())]
            for text in texts
        ]


async def test_semantic_recall_through_the_runtime(store):
    store, scope = store
    agent = make_agent(
        enabled_tools={MEMORY_WRITE, MEMORY_RECALL},
        tool_permissions={"memory.write", "memory.read"},
        learned_memory_policy=LearnedMemoryPolicy(
            enabled=True,
            recall="semantic",
            embedding_provider="embeddings",
            embedding_model="concepts",
            min_score=0.5,
        ),
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
    )
    write = {
        "expected_revision": 0,
        "kind": "procedure",
        "evidence": "The user said so",
        "retention_seconds": 3600,
    }
    provider = MockModelProvider(
        [
            tool_call_response(
                MEMORY_WRITE, {"key": "billing", "content": "Billing statement format", **write}
            ),
            tool_call_response(MEMORY_WRITE, {"key": "rain", "content": "Rain plans", **write}),
            "Saved",
        ]
    )
    runtime = make_runtime(provider, agent, learned_memory_store=store)
    runtime.router.register_provider(Embeddings())
    run = RunRequest(agent="test-agent", input="x", tenant_id=scope.tenant_id, user_id="user")
    assert (await runtime.run(run)).status == RunStatus.COMPLETED
    state = RunState(request=run)
    await runtime.runs.create(state)
    context = ToolContext(
        run_id=state.id,
        step_id="tool-recall",
        tenant_id=scope.tenant_id,
        user_id="user",
        permissions={"memory.read"},
        idempotency_key=f"{state.id}:recall",
    )
    result = await runtime.tool_executor.execute(
        MEMORY_RECALL, {"query": "invoice layout"}, context
    )
    assert [r["key"] for r in result.value["records"]] == ["billing"]
