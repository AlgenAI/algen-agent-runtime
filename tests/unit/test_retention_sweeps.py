"""Retention for conversations, workflow checkpoints, analytical graphs and work items."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from algen_agent_runtime.analytics.contracts import (
    AnalyticalGraph,
    AnalyticalGraphState,
    AnalyticalNode,
    GraphStatus,
)
from algen_agent_runtime.analytics.stores import InMemoryAnalyticalGraphStore
from algen_agent_runtime.config.settings import AppSettings, MaintenanceSettings
from algen_agent_runtime.conversations.contracts import (
    Conversation,
    ConversationEvent,
    ConversationMessage,
    MessageStatus,
)
from algen_agent_runtime.conversations.feedback import (
    ConversationFeedback,
    FeedbackRating,
    InMemoryConversationFeedbackStore,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.distributed import InMemoryWorkQueue, WorkItem
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.retention import (
    CompositeRetentionSweeper,
    InMemoryRetentionSweeper,
    PostgresRetentionSweeper,
    RetentionPolicy,
    RetentionReport,
)
from algen_agent_runtime.types.contracts import Role, TextBlock, utc_now
from algen_agent_runtime.workflows import (
    InMemoryWorkflowCheckpointStore,
    WorkflowExecutionState,
    WorkflowStatus,
)

OLD = timedelta(days=40)
WINDOW = timedelta(days=30)


async def _conversation(
    store: InMemoryConversationStore,
    events: InMemoryConversationEventBus,
    feedback: InMemoryConversationFeedbackStore,
    *,
    age: timedelta,
    status: MessageStatus = MessageStatus.COMPLETED,
    message_age: timedelta | None = None,
) -> str:
    then = utc_now() - age
    conversation = Conversation(tenant_id="t", user_id="u", agent="a", updated_at=then)
    await store.create(conversation)
    message = ConversationMessage(
        conversation_id=conversation.id,
        tenant_id="t",
        role=Role.ASSISTANT,
        content=(TextBlock(text="hello"),),
        status=status,
        created_at=utc_now() - (message_age if message_age is not None else age),
    )
    await store.append_message(message)
    await events.publish(
        ConversationEvent(
            type="message.completed",
            conversation_id=conversation.id,
            tenant_id="t",
            message_id=message.id,
            sequence=await events.next_sequence(conversation.id),
        )
    )
    await feedback.put(
        ConversationFeedback(
            tenant_id="t",
            user_id="u",
            conversation_id=conversation.id,
            message_id=message.id,
            rating=FeedbackRating.UP,
        )
    )
    return conversation.id


async def test_idle_conversations_go_with_their_messages_events_and_feedback() -> None:
    store = InMemoryConversationStore()
    events = InMemoryConversationEventBus()
    feedback = InMemoryConversationFeedbackStore()
    idle = await _conversation(store, events, feedback, age=OLD)
    recent = await _conversation(store, events, feedback, age=timedelta(days=1))
    waiting = await _conversation(
        store, events, feedback, age=OLD, status=MessageStatus.AWAITING_APPROVAL
    )
    # Its own updated_at is old, but a message is recent: still in use.
    active = await _conversation(store, events, feedback, age=OLD, message_age=timedelta(hours=1))
    sweeper = InMemoryRetentionSweeper(
        conversations=store, conversation_events=events, conversation_feedback=feedback
    )

    report = await sweeper.sweep(RetentionPolicy(conversations=WINDOW))

    assert report.deleted == {
        "conversations": 1,
        "conversation_messages": 1,
        "conversation_events": 1,
        "conversation_feedback": 1,
    }
    assert await store.get(idle, "t") is None
    assert await events.history(idle) == ()
    assert await feedback.list_for_conversation("t", "u", idle) == ()
    for kept in (recent, waiting, active):
        assert await store.get(kept, "t") is not None
    # Nothing is deleted without a window.
    assert (await sweeper.sweep(RetentionPolicy())).deleted == {}


def _workflow(status: WorkflowStatus, age: timedelta, **values: Any) -> WorkflowExecutionState:
    return WorkflowExecutionState(
        id=values.pop("id"),
        manifest_name="m",
        manifest_version="1",
        tenant_id="t",
        user_id="u",
        status=status,
        updated_at=utc_now() - age,
        **values,
    )


async def test_finished_workflow_checkpoints_go_unless_their_parent_is_unfinished() -> None:
    store = InMemoryWorkflowCheckpointStore()
    await store.create(_workflow(WorkflowStatus.COMPLETED, OLD, id="done"))
    await store.create(_workflow(WorkflowStatus.FAILED, timedelta(days=1), id="recent"))
    await store.create(_workflow(WorkflowStatus.RUNNING, OLD, id="parent"))
    await store.create(
        _workflow(WorkflowStatus.COMPLETED, OLD, id="child", parent_workflow_id="parent")
    )
    await store.create(_workflow(WorkflowStatus.AWAITING_APPROVAL, OLD, id="paused"))

    report = await InMemoryRetentionSweeper(workflow_checkpoints=store).sweep(
        RetentionPolicy(workflow_checkpoints=WINDOW)
    )

    assert report.deleted == {"workflow_checkpoints": 1}
    assert await store.get("done", "t") is None
    for kept in ("recent", "parent", "child", "paused"):
        assert await store.get(kept, "t") is not None


def _graph(status: GraphStatus, age: timedelta) -> AnalyticalGraphState:
    graph = AnalyticalGraph(
        name="g",
        version="1",
        nodes=(AnalyticalNode(id="n", kind="semantic_query", configuration={"rows": []}),),
    )
    return AnalyticalGraphState(
        id=f"graph-{status.value}-{age.days}",
        tenant_id="t",
        user_id="u",
        graph=graph,
        status=status,
        updated_at=utc_now() - age,
    )


async def test_finished_graphs_and_work_items_go_after_the_window() -> None:
    graphs = InMemoryAnalyticalGraphStore()
    for state in (
        _graph(GraphStatus.COMPLETED, OLD),
        _graph(GraphStatus.CANCELLED, OLD),
        _graph(GraphStatus.RUNNING, OLD),
        _graph(GraphStatus.FAILED, timedelta(days=1)),
    ):
        await graphs.create(state)
    queue = InMemoryWorkQueue()
    done, _ = await queue.enqueue(
        WorkItem(tenant_id="t", kind="k", deduplication_key="d1", payload={})
    )
    pending, _ = await queue.enqueue(
        WorkItem(tenant_id="t", kind="k", deduplication_key="d2", payload={})
    )
    claimed = await queue.claim("worker", 30)
    assert claimed is not None and claimed.id == done.id
    await queue.complete(done.id, "worker", {"ok": True})
    queue._items[done.id].updated_at = utc_now() - OLD

    report = await InMemoryRetentionSweeper(analytical_graphs=graphs, work_items=queue).sweep(
        RetentionPolicy(analytical_graphs=WINDOW, work_items=WINDOW, batch_size=1, max_batches=5)
    )

    assert report.deleted == {"analytical_graphs": 2, "work_items": 1}
    assert await graphs.get("graph-running-40", "t") is not None
    assert await graphs.get("graph-completed-40", "t") is None
    # The finished item's deduplication key is released; the pending one is untouched.
    again, created = await queue.enqueue(
        WorkItem(tenant_id="t", kind="k", deduplication_key="d1", payload={})
    )
    assert created and again.id != done.id
    _, created = await queue.enqueue(
        WorkItem(tenant_id="t", kind="k", deduplication_key="d2", payload={})
    )
    assert not created and pending.id in queue._items


class _Fixed:
    def __init__(self, deleted: dict[str, int]) -> None:
        self.deleted = deleted

    async def sweep(self, policy: RetentionPolicy) -> RetentionReport:
        return RetentionReport(deleted=dict(self.deleted))


async def test_composite_sweeper_adds_the_reports() -> None:
    sweeper = CompositeRetentionSweeper([_Fixed({"memory": 2}), _Fixed({"memory": 1, "x": 3})])
    assert (await sweeper.sweep(RetentionPolicy())).deleted == {"memory": 3, "x": 3}


class FakeDatabase:
    def __init__(self, deleted_per_call: int) -> None:
        self.deleted_per_call = deleted_per_call
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *arguments: Any) -> str:
        self.statements.append((query, arguments))
        return f"DELETE {self.deleted_per_call}"

    async def fetchrow(self, query: str, *arguments: Any) -> dict[str, int]:
        self.statements.append((query, arguments))
        n = self.deleted_per_call
        return {
            "conversations": n,
            "conversation_messages": n * 3,
            "conversation_events": n * 5,
            "conversation_feedback": 0,
        }


async def test_postgres_sweeper_batches_the_new_windows() -> None:
    database = FakeDatabase(deleted_per_call=10)
    policy = RetentionPolicy(
        conversations=timedelta(days=30),
        workflow_checkpoints=timedelta(days=7),
        analytical_graphs=timedelta(days=7),
        work_items=timedelta(days=1),
        batch_size=10,
        max_batches=2,
    )

    report = await PostgresRetentionSweeper(database).sweep(policy)

    assert report.deleted == {
        "memory": 20,
        "conversations": 20,
        "conversation_messages": 60,
        "conversation_events": 100,
        "conversation_feedback": 0,
        "workflow_checkpoints": 20,
        "analytical_graphs": 20,
        "work_items": 20,
    }
    calls = {
        table: [args for query, args in database.statements if table in query.split("WHERE")[0]]
        for table in (
            "algen_agent_runtime_workflow_checkpoints",
            "algen_agent_runtime_analytical_graphs",
            "algen_agent_runtime_work_items",
        )
    }
    assert calls["algen_agent_runtime_workflow_checkpoints"] == [(7 * 86_400, 10)] * 2
    assert calls["algen_agent_runtime_work_items"] == [(86_400, 10)] * 2


def test_maintenance_settings_map_to_the_policy() -> None:
    settings = AppSettings(
        maintenance=MaintenanceSettings(
            conversation_retention_days=90,
            workflow_checkpoint_retention_days=30,
            analytical_graph_retention_days=14,
            work_item_retention_days=7,
        )
    )
    maintenance = build_container(settings).maintenance
    assert maintenance is not None
    policy = maintenance.policy
    assert policy.conversations == timedelta(days=90)
    assert policy.workflow_checkpoints == timedelta(days=30)
    assert policy.analytical_graphs == timedelta(days=14)
    assert policy.work_items == timedelta(days=7)
    assert MaintenanceSettings().conversation_retention_days is None


async def test_in_memory_container_sweeps_its_own_stores() -> None:
    settings = AppSettings(maintenance=MaintenanceSettings(conversation_retention_days=30))
    container = build_container(settings)
    store = container.conversations.store
    conversation = Conversation(
        tenant_id="t", user_id="u", agent="a", updated_at=utc_now() - timedelta(days=31)
    )
    await store.create(conversation)
    assert container.maintenance is not None

    report = await container.maintenance.run_once()

    assert report.errors == {}
    assert report.deleted["conversations"] == 1
    assert await store.get(conversation.id, "t") is None
