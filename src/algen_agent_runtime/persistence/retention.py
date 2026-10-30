from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.types.contracts import utc_now


class RetentionPolicy(BaseModel):
    """Age limits applied by a retention sweep; ``None`` keeps records indefinitely."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    terminal_runs: timedelta | None = None
    audit_events: timedelta | None = None
    # Conversations not updated, and with no message, within the window and no turn in progress
    # or waiting; deleted with their messages, events and feedback.
    conversations: timedelta | None = None
    # Completed, failed and cancelled workflow runs, unless their parent run is unfinished.
    workflow_checkpoints: timedelta | None = None
    # Completed, failed and cancelled analytical graphs.
    analytical_graphs: timedelta | None = None
    # Completed and failed (dead) distributed work items.
    work_items: timedelta | None = None
    batch_size: int = Field(default=500, ge=1, le=10_000)
    max_batches: int = Field(default=20, ge=1, le=1_000)


class RetentionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: dict[str, int] = Field(default_factory=dict)

    def add(self, name: str, count: int) -> None:
        self.deleted[name] = self.deleted.get(name, 0) + count


@runtime_checkable
class RetentionSweeper(Protocol):
    async def sweep(self, policy: RetentionPolicy) -> RetentionReport: ...


class CompositeRetentionSweeper:
    """Runs several sweepers (for example PostgreSQL and in-memory stores) as one."""

    def __init__(self, sweepers: Sequence[RetentionSweeper]) -> None:
        self._sweepers = tuple(sweepers)

    async def sweep(self, policy: RetentionPolicy) -> RetentionReport:
        report = RetentionReport()
        for sweeper in self._sweepers:
            for name, count in (await sweeper.sweep(policy)).deleted.items():
                report.add(name, count)
        return report


# Data-modifying CTEs share one snapshot and PostgreSQL checks the NO ACTION foreign keys
# at the end of the statement, so children and their parent runs are removed atomically.
# Runs with unresolved side effects or a live parent workflow are retained for operators.
_DELETE_TERMINAL_RUNS = """
WITH doomed AS (
    SELECT r.id FROM algen_agent_runtime_runs r
    WHERE r.state->>'status' IN ('completed', 'failed', 'cancelled', 'timed_out')
      AND r.updated_at < now() - ($1 * interval '1 second')
      AND NOT EXISTS (
          SELECT 1 FROM algen_agent_runtime_tool_executions t
          WHERE t.run_id = r.id
            AND t.status IN ('started', 'indeterminate', 'compensating', 'compensation_failed')
      )
      AND NOT EXISTS (
          SELECT 1 FROM algen_agent_runtime_workflow_checkpoints w
          WHERE w.tenant_id = r.tenant_id
            AND w.id = r.state->'request'->>'workflow_run_id'
            AND w.status NOT IN ('completed', 'failed', 'cancelled')
      )
    ORDER BY r.updated_at
    LIMIT $2
    FOR UPDATE OF r SKIP LOCKED
),
deleted_events AS (
    DELETE FROM algen_agent_runtime_events e USING doomed d WHERE e.run_id = d.id RETURNING 1
),
deleted_approvals AS (
    DELETE FROM algen_agent_runtime_approvals a USING doomed d WHERE a.run_id = d.id RETURNING 1
),
deleted_tool_executions AS (
    DELETE FROM algen_agent_runtime_tool_executions t USING doomed d
    WHERE t.run_id = d.id RETURNING 1
),
deleted_runs AS (
    DELETE FROM algen_agent_runtime_runs r USING doomed d WHERE r.id = d.id RETURNING 1
)
SELECT
    (SELECT count(*) FROM deleted_runs) AS runs,
    (SELECT count(*) FROM deleted_events) AS events,
    (SELECT count(*) FROM deleted_approvals) AS approvals,
    (SELECT count(*) FROM deleted_tool_executions) AS tool_executions
"""

# A conversation is kept while a turn is in progress or waits for someone, and while any of its
# messages is newer than the window, whatever its own updated_at says.
_DELETE_CONVERSATIONS = """
WITH doomed AS (
    SELECT c.id FROM algen_agent_runtime_conversations c
    WHERE c.updated_at < now() - ($1 * interval '1 second')
      AND NOT EXISTS (
          SELECT 1 FROM algen_agent_runtime_conversation_messages m
          WHERE m.conversation_id = c.id
            AND (m.created_at >= now() - ($1 * interval '1 second')
                 OR m.message->>'status' IN
                    ('accepted', 'streaming', 'awaiting_approval', 'awaiting_input'))
      )
    ORDER BY c.updated_at
    LIMIT $2
    FOR UPDATE OF c SKIP LOCKED
),
deleted_feedback AS (
    DELETE FROM algen_agent_runtime_conversation_feedback f USING doomed d
    WHERE f.conversation_id = d.id RETURNING 1
),
deleted_events AS (
    DELETE FROM algen_agent_runtime_conversation_events e USING doomed d
    WHERE e.conversation_id = d.id RETURNING 1
),
deleted_messages AS (
    DELETE FROM algen_agent_runtime_conversation_messages m USING doomed d
    WHERE m.conversation_id = d.id RETURNING 1
),
deleted_conversations AS (
    DELETE FROM algen_agent_runtime_conversations c USING doomed d WHERE c.id = d.id RETURNING 1
)
SELECT
    (SELECT count(*) FROM deleted_conversations) AS conversations,
    (SELECT count(*) FROM deleted_messages) AS conversation_messages,
    (SELECT count(*) FROM deleted_events) AS conversation_events,
    (SELECT count(*) FROM deleted_feedback) AS conversation_feedback
"""

# A finished child run is kept while its parent is unfinished: the parent's recovery reads it.
_DELETE_WORKFLOW_CHECKPOINTS = """
DELETE FROM algen_agent_runtime_workflow_checkpoints w USING (
    SELECT c.tenant_id, c.id FROM algen_agent_runtime_workflow_checkpoints c
    WHERE c.status IN ('completed', 'failed', 'cancelled')
      AND c.updated_at < now() - ($1 * interval '1 second')
      AND NOT EXISTS (
          SELECT 1 FROM algen_agent_runtime_workflow_checkpoints p
          WHERE p.tenant_id = c.tenant_id
            AND p.id = c.state->>'parent_workflow_id'
            AND p.status NOT IN ('completed', 'failed', 'cancelled')
      )
    ORDER BY c.updated_at
    LIMIT $2
    FOR UPDATE OF c SKIP LOCKED
) doomed
WHERE w.tenant_id = doomed.tenant_id AND w.id = doomed.id
"""

_DELETE_ANALYTICAL_GRAPHS = (
    "DELETE FROM algen_agent_runtime_analytical_graphs WHERE id IN "
    "(SELECT id FROM algen_agent_runtime_analytical_graphs "
    "WHERE status IN ('completed', 'failed', 'cancelled') "
    "AND updated_at < now() - ($1 * interval '1 second') "
    "ORDER BY updated_at LIMIT $2 FOR UPDATE SKIP LOCKED)"
)

_DELETE_WORK_ITEMS = (
    "DELETE FROM algen_agent_runtime_work_items WHERE id IN "
    "(SELECT id FROM algen_agent_runtime_work_items "
    "WHERE status IN ('completed', 'failed') "
    "AND updated_at < now() - ($1 * interval '1 second') "
    "ORDER BY updated_at LIMIT $2 FOR UPDATE SKIP LOCKED)"
)

_DELETE_EXPIRED_MEMORY = (
    "DELETE FROM algen_agent_runtime_memory WHERE id IN "
    "(SELECT id FROM algen_agent_runtime_memory "
    "WHERE expires_at IS NOT NULL AND expires_at <= now() "
    "ORDER BY expires_at LIMIT $1 FOR UPDATE SKIP LOCKED)"
)

_DELETE_AUDIT_EVENTS = (
    "DELETE FROM algen_agent_runtime_audit_events WHERE id IN "
    "(SELECT id FROM algen_agent_runtime_audit_events "
    "WHERE created_at < now() - ($1 * interval '1 second') "
    "ORDER BY created_at LIMIT $2 FOR UPDATE SKIP LOCKED)"
)


class PostgresRetentionSweeper:
    """Batched deletion of expired PostgreSQL runtime records.

    Expired memory rows are always removed because readers already ignore them. Terminal
    runs, audit events, conversations, workflow checkpoints, analytical graphs and work items are
    removed only when the policy sets an age limit for them.
    """

    def __init__(self, database: Any) -> None:
        self._database = database

    async def sweep(self, policy: RetentionPolicy) -> RetentionReport:
        report = RetentionReport()
        await self._batched(
            policy,
            lambda: self._delete_count(_DELETE_EXPIRED_MEMORY, policy.batch_size),
            report,
            "memory",
        )
        if policy.terminal_runs is not None:
            seconds = int(policy.terminal_runs.total_seconds())
            for _ in range(policy.max_batches):
                row = await self._database.fetchrow(
                    _DELETE_TERMINAL_RUNS, seconds, policy.batch_size
                )
                counts = {key: int(row[key]) for key in _RUN_COUNT_KEYS}
                for key, value in counts.items():
                    report.add(key, value)
                if counts["runs"] < policy.batch_size:
                    break
        if policy.audit_events is not None:
            seconds = int(policy.audit_events.total_seconds())
            await self._batched(
                policy,
                lambda: self._delete_count(_DELETE_AUDIT_EVENTS, seconds, policy.batch_size),
                report,
                "audit_events",
            )
        if policy.conversations is not None:
            seconds = int(policy.conversations.total_seconds())
            for _ in range(policy.max_batches):
                row = await self._database.fetchrow(
                    _DELETE_CONVERSATIONS, seconds, policy.batch_size
                )
                counts = {key: int(row[key]) for key in _CONVERSATION_COUNT_KEYS}
                for key, value in counts.items():
                    report.add(key, value)
                if counts["conversations"] < policy.batch_size:
                    break
        for name, window, query in (
            ("workflow_checkpoints", policy.workflow_checkpoints, _DELETE_WORKFLOW_CHECKPOINTS),
            ("analytical_graphs", policy.analytical_graphs, _DELETE_ANALYTICAL_GRAPHS),
            ("work_items", policy.work_items, _DELETE_WORK_ITEMS),
        ):
            if window is None:
                continue
            seconds = int(window.total_seconds())
            await self._batched(
                policy,
                lambda query=query, seconds=seconds: self._delete_count(
                    query, seconds, policy.batch_size
                ),
                report,
                name,
            )
        return report

    async def _batched(
        self, policy: RetentionPolicy, delete: Any, report: RetentionReport, name: str
    ) -> None:
        report.add(name, 0)
        for _ in range(policy.max_batches):
            count = await delete()
            report.add(name, count)
            if count < policy.batch_size:
                return

    async def _delete_count(self, query: str, *arguments: Any) -> int:
        result = await self._database.execute(query, *arguments)
        return int(str(result).rsplit(" ", 1)[-1])


_RUN_COUNT_KEYS = ("runs", "events", "approvals", "tool_executions")
_CONVERSATION_COUNT_KEYS = (
    "conversations",
    "conversation_messages",
    "conversation_events",
    "conversation_feedback",
)


class InMemoryRetentionSweeper:
    """Applies the conversation, workflow, analytical graph and work item windows to in-memory
    stores; pass the stores the deployment keeps in memory and ``None`` for the others."""

    def __init__(
        self,
        *,
        conversations: Any | None = None,
        conversation_events: Any | None = None,
        conversation_feedback: Any | None = None,
        workflow_checkpoints: Any | None = None,
        analytical_graphs: Any | None = None,
        work_items: Any | None = None,
    ) -> None:
        self._conversations = conversations
        self._conversation_events = conversation_events
        self._conversation_feedback = conversation_feedback
        self._workflow_checkpoints = workflow_checkpoints
        self._analytical_graphs = analytical_graphs
        self._work_items = work_items

    async def sweep(self, policy: RetentionPolicy) -> RetentionReport:
        report = RetentionReport()
        limit = policy.batch_size * policy.max_batches
        conversations = self._conversations
        if policy.conversations is not None and conversations is not None:
            cutoff = utc_now() - policy.conversations
            ids, messages = await conversations.purge_inactive(cutoff, limit)
            report.add("conversations", len(ids))
            report.add("conversation_messages", messages)
            if self._conversation_events is not None:
                report.add("conversation_events", await self._conversation_events.forget(ids))
            if self._conversation_feedback is not None:
                report.add(
                    "conversation_feedback",
                    await self._conversation_feedback.forget_conversations(ids),
                )
        for name, window, store in (
            ("workflow_checkpoints", policy.workflow_checkpoints, self._workflow_checkpoints),
            ("analytical_graphs", policy.analytical_graphs, self._analytical_graphs),
            ("work_items", policy.work_items, self._work_items),
        ):
            if window is not None and store is not None:
                report.add(name, await store.purge_finished(utc_now() - window, limit))
        return report
