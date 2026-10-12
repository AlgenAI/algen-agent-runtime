from __future__ import annotations

from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class RetentionPolicy(BaseModel):
    """Age limits applied by a retention sweep; ``None`` keeps records indefinitely."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    terminal_runs: timedelta | None = None
    audit_events: timedelta | None = None
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
          WHERE t.run_id = r.id AND t.status IN ('started', 'indeterminate')
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
    runs and audit events are removed only when the policy sets an age limit for them.
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
