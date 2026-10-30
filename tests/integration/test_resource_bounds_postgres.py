"""Storage quotas, usage accounting and the new retention windows on a live PostgreSQL."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.analytics.contracts import (
    AnalyticalGraph,
    AnalyticalGraphState,
    AnalyticalNode,
    GraphStatus,
)
from algen_agent_runtime.analytics.stores import PostgresAnalyticalGraphStore
from algen_agent_runtime.conversations.contracts import (
    Conversation,
    ConversationEvent,
    ConversationMessage,
    MessageStatus,
)
from algen_agent_runtime.conversations.feedback import (
    ConversationFeedback,
    FeedbackRating,
    PostgresConversationFeedbackStore,
)
from algen_agent_runtime.conversations.stores import (
    PostgresConversationEventBus,
    PostgresConversationStore,
)
from algen_agent_runtime.distributed import PostgresWorkQueue, WorkItem
from algen_agent_runtime.events.bus import PostgresEventBus
from algen_agent_runtime.exceptions.errors import QuotaExceededError
from algen_agent_runtime.memory.contracts import LearnedMemory, MemoryMutation, MemoryScope
from algen_agent_runtime.memory.store import PostgresLearnedMemoryStore
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
)
from algen_agent_runtime.persistence.quotas import (
    ResourceUsage,
    StorageQuota,
    StorageQuotas,
    StorageQuotaSettings,
)
from algen_agent_runtime.persistence.retention import PostgresRetentionSweeper, RetentionPolicy
from algen_agent_runtime.tools.starter.clock import clock_tool
from algen_agent_runtime.types.contracts import (
    Artifact,
    Budget,
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    RunRequest,
    RunStatus,
    TextBlock,
    TokenUsage,
    ToolCall,
    utc_now,
)
from algen_agent_runtime.workflows import (
    PostgresWorkflowCheckpointStore,
    WorkflowExecutionState,
    WorkflowStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(not os.environ.get("POSTGRES_DSN"), reason="requires a live PostgreSQL"),
]

OLD = timedelta(days=40)
WINDOW = timedelta(days=30)


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"], max_pool_size=20)
    await db.start()
    try:
        yield db
    finally:
        await db.close()


def _quotas(**default: Any) -> StorageQuotas:
    return StorageQuotas(StorageQuotaSettings(default=StorageQuota(**default)))


def _artifact(tenant: str, size: int, **values: Any) -> Artifact:
    return Artifact(
        tenant_id=tenant,
        name="a.bin",
        media_type="application/octet-stream",
        data=b"x" * size,
        **values,
    )


async def _usage(db: PostgresDatabase, tenant: str, resource: str) -> ResourceUsage:
    row = await db.fetchrow(
        "SELECT items, bytes FROM algen_agent_runtime_tenant_usage "
        "WHERE tenant_id=$1 AND resource=$2",
        tenant,
        resource,
    )
    return ResourceUsage(int(row["items"]), int(row["bytes"])) if row else ResourceUsage()


async def test_triggers_track_artifact_usage_on_every_write_path(
    database: PostgresDatabase,
) -> None:
    tenant = f"quota-{uuid4()}"
    store = PostgresArtifactStore(database)
    first = _artifact(tenant, 100)
    expired = _artifact(tenant, 40, expires_at=utc_now() - timedelta(seconds=1))
    await store.put(first)
    await store.put(expired)
    assert await store.tenant_usage(tenant) == ResourceUsage(2, 140)

    await store.purge_expired(limit=10_000)
    assert await store.tenant_usage(tenant) == ResourceUsage(1, 100)
    # A delete by hand (an operator, another tool) is counted too.
    await database.execute("DELETE FROM algen_agent_runtime_artifacts WHERE id=$1", first.id)
    assert await store.tenant_usage(tenant) == ResourceUsage(0, 0)


async def test_artifact_quota_refuses_without_writing(database: PostgresDatabase) -> None:
    tenant = f"quota-{uuid4()}"
    store = PostgresArtifactStore(database, quotas=_quotas(artifact_count=2, artifact_bytes=150))
    await store.put(_artifact(tenant, 100))
    refused = _artifact(tenant, 60)
    with pytest.raises(QuotaExceededError) as raised:
        await store.put(refused)
    assert (raised.value.dimension, raised.value.limit, raised.value.used) == ("bytes", 150, 160)
    assert await store.describe(refused.id, tenant) is None
    assert await store.tenant_usage(tenant) == ResourceUsage(1, 100)
    # Other tenants are unaffected.
    await store.put(_artifact(f"quota-{uuid4()}", 150))


async def test_concurrent_writes_never_overshoot_the_quota(database: PostgresDatabase) -> None:
    tenant = f"quota-{uuid4()}"
    store = PostgresArtifactStore(database, quotas=_quotas(artifact_count=5))

    outcomes = await asyncio.gather(
        *(store.put(_artifact(tenant, 10)) for _ in range(20)), return_exceptions=True
    )

    assert sum(outcome is None for outcome in outcomes) == 5
    assert all(outcome is None or isinstance(outcome, QuotaExceededError) for outcome in outcomes)
    assert await store.tenant_usage(tenant) == ResourceUsage(5, 50)
    row = await database.fetchrow(
        "SELECT count(*) AS n FROM algen_agent_runtime_artifacts WHERE tenant_id=$1", tenant
    )
    assert row["n"] == 5


async def test_session_memory_quota_and_expiry_purge(database: PostgresDatabase) -> None:
    tenant = f"quota-{uuid4()}"
    store = PostgresMemoryStore(database, quotas=_quotas(memory_items=3))
    await store.append(tenant, "s1", [Message.text(Role.USER, "1"), Message.text(Role.USER, "2")])
    with pytest.raises(QuotaExceededError):
        await store.append(
            tenant, "s2", [Message.text(Role.USER, "3"), Message.text(Role.USER, "4")]
        )
    # All or nothing: the refused append wrote neither message.
    assert await store.get(tenant, "s2", 10) == ()
    usage = await store.tenant_usage(tenant)
    assert usage.items == 2 and usage.bytes > 0

    await database.execute(
        "UPDATE algen_agent_runtime_memory SET expires_at = now() - interval '1 second' "
        "WHERE tenant_id=$1",
        tenant,
    )
    await PostgresRetentionSweeper(database).sweep(RetentionPolicy(batch_size=10_000))
    assert await store.tenant_usage(tenant) == ResourceUsage(0, 0)
    await store.append(tenant, "s2", [Message.text(Role.USER, "3")])


def _mutation(scope: MemoryScope, key: str, revision: int, content: str | None) -> MemoryMutation:
    return MemoryMutation(
        scope=scope,
        operation_id=f"{key}-{revision}",
        expected_revision=revision - 1,
        record=LearnedMemory(
            key=key,
            revision=revision,
            kind="preference" if content is not None else None,
            content=content,
            evidence="the user said so" if content is not None else None,
            deleted=content is None,
            source_run_id="run",
            source_step_id="step",
            source_agent_version="1.0.0",
            updated_at=utc_now(),
            expires_at=utc_now() + timedelta(days=1) if content is not None else None,
        ),
    )


async def test_learned_memory_quota_counts_live_records(database: PostgresDatabase) -> None:
    tenant = f"quota-{uuid4()}"
    store = PostgresLearnedMemoryStore(database, quotas=_quotas(learned_memory_items=1))
    scope = MemoryScope(tenant_id=tenant, user_id="u", agent_id="agent")
    await store.apply(_mutation(scope, "first", 1, "likes tea"))
    with pytest.raises(QuotaExceededError):
        await store.apply(_mutation(scope, "second", 1, "likes coffee"))
    assert await store.get(scope, "second") is None
    await store.apply(_mutation(scope, "first", 2, "likes green tea"))
    await store.apply(_mutation(scope, "first", 3, None))
    assert await store.tenant_usage(tenant) == ResourceUsage(0, 0)
    await store.apply(_mutation(scope, "second", 1, "likes coffee"))
    assert (await store.tenant_usage(tenant)).items == 1

    # The expiry purge clears content, so the record stops counting.
    await database.execute(
        "UPDATE algen_agent_runtime_learned_memory SET expires_at = now() - interval '1 second' "
        "WHERE tenant_id=$1",
        tenant,
    )
    await store.purge_expired(10_000)
    assert await store.tenant_usage(tenant) == ResourceUsage(0, 0)


async def _conversation(
    db: PostgresDatabase,
    tenant: str,
    *,
    age: timedelta,
    status: MessageStatus = MessageStatus.COMPLETED,
    message_age: timedelta | None = None,
) -> str:
    store = PostgresConversationStore(db)
    conversation = Conversation(tenant_id=tenant, user_id="u", agent="a")
    await store.create(conversation)
    message = ConversationMessage(
        conversation_id=conversation.id,
        tenant_id=tenant,
        role=Role.ASSISTANT,
        content=(TextBlock(text="hello"),),
        status=status,
    )
    await store.append_message(message)
    events = PostgresConversationEventBus(db)
    await events.publish(
        ConversationEvent(
            type="message.completed",
            conversation_id=conversation.id,
            tenant_id=tenant,
            message_id=message.id,
            sequence=await events.next_sequence(conversation.id),
        )
    )
    await PostgresConversationFeedbackStore(db).put(
        ConversationFeedback(
            tenant_id=tenant,
            user_id="u",
            conversation_id=conversation.id,
            message_id=message.id,
            rating=FeedbackRating.UP,
        )
    )
    await db.execute(
        "UPDATE algen_agent_runtime_conversations "
        "SET updated_at = now() - ($2 * interval '1 second') WHERE id=$1",
        conversation.id,
        int(age.total_seconds()),
    )
    await db.execute(
        "UPDATE algen_agent_runtime_conversation_messages "
        "SET created_at = now() - ($2 * interval '1 second') WHERE conversation_id=$1",
        conversation.id,
        int((message_age if message_age is not None else age).total_seconds()),
    )
    return conversation.id


async def _children(db: PostgresDatabase, conversation_id: str) -> tuple[int, int, int]:
    counts = []
    for table in (
        "algen_agent_runtime_conversation_messages",
        "algen_agent_runtime_conversation_events",
        "algen_agent_runtime_conversation_feedback",
    ):
        row = await db.fetchrow(
            f"SELECT count(*) AS n FROM {table} WHERE conversation_id=$1", conversation_id
        )
        counts.append(int(row["n"]))
    return counts[0], counts[1], counts[2]


async def test_conversation_retention_deletes_idle_conversations_with_children(
    database: PostgresDatabase,
) -> None:
    tenant = f"retention-{uuid4()}"
    idle = await _conversation(database, tenant, age=OLD)
    recent = await _conversation(database, tenant, age=timedelta(days=1))
    waiting = await _conversation(database, tenant, age=OLD, status=MessageStatus.AWAITING_INPUT)
    streaming = await _conversation(database, tenant, age=OLD, status=MessageStatus.STREAMING)
    active = await _conversation(database, tenant, age=OLD, message_age=timedelta(hours=1))

    report = await PostgresRetentionSweeper(database).sweep(
        RetentionPolicy(conversations=WINDOW, batch_size=1_000)
    )

    assert report.deleted["conversations"] >= 1
    assert report.deleted["conversation_feedback"] >= 1
    store = PostgresConversationStore(database)
    assert await store.get(idle, tenant) is None
    assert await _children(database, idle) == (0, 0, 0)
    for kept in (recent, waiting, streaming, active):
        assert await store.get(kept, tenant) is not None, kept
        assert await _children(database, kept) == (1, 1, 1)


def _workflow(tenant: str, status: WorkflowStatus, **values: Any) -> WorkflowExecutionState:
    return WorkflowExecutionState(
        id=values.pop("id", f"wf-{uuid4()}"),
        manifest_name="wf",
        manifest_version="1.0.0",
        tenant_id=tenant,
        user_id="u",
        status=status,
        **values,
    )


async def _age(db: PostgresDatabase, table: str, column: str, value: str, age: timedelta) -> None:
    await db.execute(
        f"UPDATE {table} SET updated_at = now() - ($2 * interval '1 second') WHERE {column}=$1",
        value,
        int(age.total_seconds()),
    )


async def test_finished_workflows_graphs_and_work_items_are_deleted_after_their_window(
    database: PostgresDatabase,
) -> None:
    tenant = f"retention-{uuid4()}"
    checkpoints = PostgresWorkflowCheckpointStore(database)
    done = _workflow(tenant, WorkflowStatus.COMPLETED)
    parent = _workflow(tenant, WorkflowStatus.RUNNING)
    child = _workflow(tenant, WorkflowStatus.FAILED, parent_workflow_id=parent.id)
    paused = _workflow(tenant, WorkflowStatus.AWAITING_APPROVAL)
    recent = _workflow(tenant, WorkflowStatus.CANCELLED)
    for state in (done, parent, child, paused, recent):
        await checkpoints.create(state)
        if state is not recent:
            await _age(database, "algen_agent_runtime_workflow_checkpoints", "id", state.id, OLD)

    graphs = PostgresAnalyticalGraphStore(database)
    graph = AnalyticalGraph(
        name="g",
        version="1",
        nodes=(AnalyticalNode(id="n", kind="semantic_query", configuration={"rows": []}),),
    )
    finished = AnalyticalGraphState(
        id=f"g-{uuid4()}", tenant_id=tenant, user_id="u", graph=graph, status=GraphStatus.COMPLETED
    )
    running = AnalyticalGraphState(
        id=f"g-{uuid4()}", tenant_id=tenant, user_id="u", graph=graph, status=GraphStatus.RUNNING
    )
    for state in (finished, running):
        await graphs.create(state)
        await _age(database, "algen_agent_runtime_analytical_graphs", "id", state.id, OLD)

    queue = PostgresWorkQueue(database)
    dead, _ = await queue.enqueue(
        WorkItem(tenant_id=tenant, kind="k", deduplication_key=f"d-{uuid4()}", payload={})
    )
    pending, _ = await queue.enqueue(
        WorkItem(tenant_id=tenant, kind="k", deduplication_key=f"d-{uuid4()}", payload={})
    )
    await database.execute(
        "UPDATE algen_agent_runtime_work_items SET status='failed' WHERE id=$1", dead.id
    )
    for item in (dead, pending):
        await _age(database, "algen_agent_runtime_work_items", "id", item.id, OLD)

    report = await PostgresRetentionSweeper(database).sweep(
        RetentionPolicy(
            workflow_checkpoints=WINDOW,
            analytical_graphs=WINDOW,
            work_items=WINDOW,
            batch_size=1_000,
        )
    )

    assert report.deleted["workflow_checkpoints"] >= 1
    assert report.deleted["analytical_graphs"] >= 1
    assert report.deleted["work_items"] >= 1
    assert await checkpoints.get(done.id, tenant) is None
    for kept in (parent, child, paused, recent):
        assert await checkpoints.get(kept.id, tenant) is not None, kept.id
    assert await graphs.get(finished.id, tenant) is None
    assert await graphs.get(running.id, tenant) is not None
    rows = await database.fetch(
        "SELECT id FROM algen_agent_runtime_work_items WHERE tenant_id=$1", tenant
    )
    assert [row["id"] for row in rows] == [pending.id]


async def test_event_limit_bounds_the_stored_events_of_a_runaway_run(
    database: PostgresDatabase,
) -> None:
    def call_forever(request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, ""),
            tool_calls=(ToolCall(name="starter.clock", arguments={}),),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
            model="deterministic",
            provider="mock",
        )

    runtime = make_runtime(
        MockModelProvider(handler=call_forever),
        make_agent(
            enabled_tools=frozenset({"starter.clock"}), max_steps=500, budget=Budget(max_events=120)
        ),
    )
    runtime.tools.register(clock_tool())
    # Run events reference their run, so both live in PostgreSQL.
    runtime.runs = PostgresRunStore(database)
    runtime.events = PostgresEventBus(database)
    tenant = f"limits-{uuid4()}"

    result = await runtime.run(
        RunRequest(agent="test-agent", input="go", tenant_id=tenant, user_id="u")
    )

    assert result.status == RunStatus.FAILED
    row = await database.fetchrow(
        "SELECT count(*) AS n, max(sequence) AS last FROM algen_agent_runtime_events "
        "WHERE run_id=$1",
        result.run_id,
    )
    assert (row["n"], row["last"]) == (121, 121)
    events = await runtime.events.history(result.run_id)
    assert events[-1].type == "run.failed"
    assert events[-1].data["limit"] == "events"
