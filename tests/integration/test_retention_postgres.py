from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from algen_agent_runtime.approvals.service import PostgresApprovalService
from algen_agent_runtime.events.bus import PostgresAuditLog, PostgresEventBus
from algen_agent_runtime.events.contracts import AuditEvent, RunEvent
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.persistence.retention import PostgresRetentionSweeper, RetentionPolicy
from algen_agent_runtime.tools.contracts import ToolContext, ToolResult
from algen_agent_runtime.types.contracts import (
    Artifact,
    Message,
    Role,
    RunRequest,
    RunState,
    RunStatus,
)
from algen_agent_runtime.workflows import (
    PostgresWorkflowCheckpointStore,
    WorkflowExecutionState,
    WorkflowStatus,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("POSTGRES_DSN"), reason="retention SQL requires a live PostgreSQL"
    ),
]

RUN_RETENTION = timedelta(days=30)


@pytest.fixture
async def database() -> AsyncIterator[PostgresDatabase]:
    pytest.importorskip("asyncpg")
    db = PostgresDatabase(os.environ["POSTGRES_DSN"])
    await db.start()
    try:
        yield db
    finally:
        await db.close()


async def _run(
    db: PostgresDatabase,
    tenant: str,
    status: RunStatus,
    *,
    age: timedelta,
    workflow_run_id: str | None = None,
) -> str:
    state = RunState(
        request=RunRequest(
            agent="test-agent",
            input="q",
            tenant_id=tenant,
            user_id="u",
            workflow_run_id=workflow_run_id,
        ),
        status=status,
    )
    await PostgresRunStore(db).create(state)
    await db.execute(
        "UPDATE algen_agent_runtime_runs SET updated_at = now() - ($2 * interval '1 second') "
        "WHERE id=$1",
        state.id,
        int(age.total_seconds()),
    )
    return state.id


async def _children(db: PostgresDatabase, tenant: str, run_id: str, *, tool_status: str) -> None:
    events = PostgresEventBus(db)
    for sequence in (1, 2):
        await events.publish(
            RunEvent(
                type="run.started",
                run_id=run_id,
                tenant_id=tenant,
                session_id="s",
                correlation_id="c",
                sequence=sequence,
            )
        )
    await PostgresApprovalService(db).create(run_id, "step", tenant, "write", {}, "high", 60)
    executions = PostgresToolExecutionStore(db)
    key = f"{run_id}:tool"
    context = ToolContext(
        run_id=run_id,
        step_id="step",
        tenant_id=tenant,
        user_id="u",
        permissions=frozenset(),
        idempotency_key=key,
    )
    await executions.begin(context, "external.write")
    if tool_status == "completed":
        await executions.complete(key, ToolResult(value={"ok": True}))
    elif tool_status == "indeterminate":
        await executions.fail(key, "connection reset", indeterminate=True)


async def _count(db: PostgresDatabase, table: str, run_id: str) -> int:
    row = await db.fetchrow(f"SELECT count(*) AS n FROM {table} WHERE run_id=$1", run_id)
    return int(row["n"])


async def _run_exists(db: PostgresDatabase, run_id: str) -> bool:
    return (
        await db.fetchrow("SELECT 1 FROM algen_agent_runtime_runs WHERE id=$1", run_id) is not None
    )


async def test_sweep_deletes_old_terminal_runs_with_children_and_keeps_protected_runs(
    database: PostgresDatabase,
) -> None:
    db = database
    tenant = f"retention-{uuid4()}"
    old = RUN_RETENTION + timedelta(days=1)

    purged = await _run(db, tenant, RunStatus.COMPLETED, age=old)
    await _children(db, tenant, purged, tool_status="completed")
    artifact = Artifact(
        tenant_id=tenant, run_id=purged, name="report", media_type="text/plain", data=b"x"
    )
    await PostgresArtifactStore(db).put(artifact)

    recent = await _run(db, tenant, RunStatus.FAILED, age=timedelta(days=1))
    active = await _run(db, tenant, RunStatus.AWAITING_APPROVAL, age=old)
    unresolved = await _run(db, tenant, RunStatus.FAILED, age=old)
    await _children(db, tenant, unresolved, tool_status="indeterminate")

    workflow = WorkflowExecutionState(
        id=f"wf-{uuid4()}",
        manifest_name="wf",
        manifest_version="1.0.0",
        tenant_id=tenant,
        user_id="u",
        status=WorkflowStatus.AWAITING_APPROVAL,
        version=0,
    )
    await PostgresWorkflowCheckpointStore(db).create(workflow)
    in_live_workflow = await _run(
        db, tenant, RunStatus.COMPLETED, age=old, workflow_run_id=workflow.id
    )

    report = await PostgresRetentionSweeper(db).sweep(
        RetentionPolicy(terminal_runs=RUN_RETENTION, batch_size=1000)
    )

    assert report.deleted["runs"] >= 1
    assert not await _run_exists(db, purged)
    for table in (
        "algen_agent_runtime_events",
        "algen_agent_runtime_approvals",
        "algen_agent_runtime_tool_executions",
    ):
        assert await _count(db, table, purged) == 0
    # Artifacts follow their own expiry; the run reference is cleared by ON DELETE SET NULL.
    kept_artifact = await PostgresArtifactStore(db).describe(artifact.id, tenant)
    assert kept_artifact is not None and kept_artifact.run_id is None

    for run_id in (recent, active, unresolved, in_live_workflow):
        assert await _run_exists(db, run_id), run_id
    assert await _count(db, "algen_agent_runtime_events", unresolved) == 2


async def test_sweep_keeps_runs_and_audit_events_when_no_age_limit_is_set(
    database: PostgresDatabase,
) -> None:
    db = database
    tenant = f"retention-{uuid4()}"
    run_id = await _run(db, tenant, RunStatus.COMPLETED, age=timedelta(days=3650))
    event = AuditEvent(
        action="model.call", outcome="allowed", tenant_id=tenant, actor_id="u", resource_id=run_id
    )
    await PostgresAuditLog(db).append(event)
    await db.execute(
        "UPDATE algen_agent_runtime_audit_events SET created_at = now() - interval '3650 days' "
        "WHERE id=$1",
        event.id,
    )

    await PostgresRetentionSweeper(db).sweep(RetentionPolicy())

    assert await _run_exists(db, run_id)
    assert await PostgresAuditLog(db).list(tenant) == (event,)

    await PostgresRetentionSweeper(db).sweep(RetentionPolicy(audit_events=timedelta(days=365)))

    assert await PostgresAuditLog(db).list(tenant) == ()


async def test_sweep_removes_only_expired_memory(database: PostgresDatabase) -> None:
    db = database
    tenant = f"retention-{uuid4()}"
    message = [Message.text(Role.USER, "hello")]
    await PostgresMemoryStore(db, retention_seconds=0).append(tenant, "expired", message)
    await PostgresMemoryStore(db, retention_seconds=3600).append(tenant, "live", message)

    await PostgresRetentionSweeper(db).sweep(RetentionPolicy())

    rows: Any = await db.fetch(
        "SELECT session_id FROM algen_agent_runtime_memory WHERE tenant_id=$1", tenant
    )
    assert [row["session_id"] for row in rows] == ["live"]


async def test_expired_postgres_approval_times_out_paused_run(database: PostgresDatabase) -> None:
    from conftest import make_runtime

    db = database
    tenant = f"retention-{uuid4()}"
    runtime = make_runtime()
    runtime.runs = PostgresRunStore(db)
    runtime.approvals = PostgresApprovalService(db)
    runtime.events = PostgresEventBus(db)
    run_id = await _run(db, tenant, RunStatus.AWAITING_APPROVAL, age=timedelta(hours=2))
    live_id = await _run(db, tenant, RunStatus.AWAITING_APPROVAL, age=timedelta(hours=2))
    for target, expires_seconds in ((run_id, 1), (live_id, 3600)):
        approval = await runtime.approvals.create(
            target, "step", tenant, "write", {}, "high", expires_seconds
        )
        state = await runtime.runs.get(target, tenant)
        assert state is not None
        state.pause_payload = {"approval_id": approval.id, "tool_call_id": "call-1"}
        await runtime.runs.save(state, state.version)
    await db.execute(
        "UPDATE algen_agent_runtime_approvals "
        "SET approval = jsonb_set(approval, '{expires_at}', to_jsonb((now() - interval '1 minute')::text)) "
        "WHERE run_id=$1",
        run_id,
    )

    await runtime.expire_paused_runs()

    expired = await runtime.runs.get(run_id, tenant)
    live = await runtime.runs.get(live_id, tenant)
    assert expired is not None and expired.status == RunStatus.TIMED_OUT
    assert live is not None and live.status == RunStatus.AWAITING_APPROVAL
    history = await runtime.events.history(run_id)
    assert history[-1].data == {"reason": "approval_expired"}
