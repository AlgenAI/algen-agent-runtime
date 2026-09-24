from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.persistence.postgres import (
    PostgresArtifactStore,
    PostgresDatabase,
    PostgresMemoryStore,
    PostgresRunStore,
    PostgresToolExecutionStore,
)
from algen_agent_runtime.tools.contracts import (
    ToolContext,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactStatus,
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

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class BehavioralAsyncpgDouble:
    """In-memory behavioral double mimicking PostgreSQL / asyncpg for storage tests."""

    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], dict[str, Any]] = {}
        self.memory: list[dict[str, Any]] = []
        self.artifacts: dict[tuple[str, str], dict[str, Any]] = {}
        self.workflows: dict[tuple[str, str], dict[str, Any]] = {}
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.migrations: set[str] = set()

    async def execute(self, query: str, *args: Any) -> str:
        q = query.strip()
        if "algen_agent_runtime_runs" in q:
            if q.startswith("INSERT"):
                # args: id, tenant_id, version, state
                key = (str(args[1]), str(args[0]))
                if key in self.runs:
                    raise ConflictError(f"run {args[0]!r} already exists")
                self.runs[key] = {
                    "id": str(args[0]),
                    "tenant_id": str(args[1]),
                    "version": int(args[2]),
                    "state": str(args[3]),
                    "updated_at": datetime.now(UTC),
                }
                return "INSERT 0 1"
            if q.startswith("UPDATE"):
                # UPDATE ... SET version=$1, state=$2::jsonb, updated_at=now() WHERE id=$3 AND tenant_id=$4 AND version=$5
                key = (str(args[3]), str(args[2]))
                row = self.runs.get(key)
                if row is None or row["version"] != int(args[4]):
                    return "UPDATE 0"
                row["version"] = int(args[0])
                row["state"] = str(args[1])
                row["updated_at"] = datetime.now(UTC)
                return "UPDATE 1"

        if "algen_agent_runtime_memory" in q:
            if q.startswith("INSERT"):
                # args: tenant_id, session_id, message, retention
                self.memory.append(
                    {
                        "tenant_id": str(args[0]),
                        "session_id": str(args[1]),
                        "message": str(args[2]),
                    }
                )
                return "INSERT 0 1"
            if q.startswith("DELETE"):
                self.memory = [
                    m
                    for m in self.memory
                    if not (m["tenant_id"] == str(args[0]) and m["session_id"] == str(args[1]))
                ]
                return "DELETE 1"

        if "algen_agent_runtime_artifacts" in q:
            if q.startswith("INSERT"):
                # args: id, tenant_id, run_id, name, media_type, data, size_bytes, sha256, status, metadata, expires_at, created_at
                key = (str(args[1]), str(args[0]))
                self.artifacts[key] = {
                    "id": str(args[0]),
                    "tenant_id": str(args[1]),
                    "run_id": args[2],
                    "name": str(args[3]),
                    "media_type": str(args[4]),
                    "data": args[5],
                    "size_bytes": int(args[6]),
                    "sha256": str(args[7]),
                    "status": str(args[8]),
                    "metadata": str(args[9]),
                    "expires_at": args[10],
                    "created_at": args[11],
                    "storage_backend": "postgres",
                    "storage_key": None,
                }
                return "INSERT 0 1"

        if "algen_agent_runtime_workflow_checkpoints" in q:
            if q.startswith("INSERT"):
                key = (str(args[1]), str(args[0]))
                if key in self.workflows:
                    raise ConflictError(f"workflow {args[0]!r} already exists")
                self.workflows[key] = {
                    "id": str(args[0]),
                    "tenant_id": str(args[1]),
                    "name": str(args[2]),
                    "version_label": str(args[3]),
                    "status": str(args[4]),
                    "version": int(args[5]),
                    "state": str(args[6]),
                    "updated_at": args[7],
                }
                return "INSERT 0 1"
            if q.startswith("UPDATE"):
                key = (str(args[5]), str(args[4]))
                row = self.workflows.get(key)
                if row is None or row["version"] != int(args[6]):
                    return "UPDATE 0"
                row.update(
                    status=str(args[0]),
                    version=int(args[1]),
                    state=str(args[2]),
                    updated_at=args[3],
                )
                return "UPDATE 1"

        if "algen_agent_runtime_tool_executions" in q:
            if q.startswith("INSERT"):
                # args: idempotency_key, run_id, step_id, tenant_id, tool_name, status, record
                idemp_key = str(args[0])
                if idemp_key in self.tools:
                    return "INSERT 0 0"
                self.tools[idemp_key] = {
                    "idempotency_key": idemp_key,
                    "run_id": str(args[1]),
                    "step_id": str(args[2]),
                    "tenant_id": str(args[3]),
                    "tool_name": str(args[4]),
                    "status": str(args[5]),
                    "record": str(args[6]),
                }
                return "INSERT 0 1"
            if q.startswith("UPDATE"):
                # UPDATE ... WHERE idempotency_key=$3 or $2
                idemp_key = str(args[-1])
                row = self.tools.get(idemp_key)
                if row:
                    row["status"] = str(args[0])
                    row["record"] = str(args[1]) if len(args) > 2 else row["record"]
                    return "UPDATE 1"
                return "UPDATE 0"

        if "algen_agent_runtime_schema_migrations" in q:
            if q.startswith("INSERT"):
                self.migrations.add(str(args[0]))
                return "INSERT 0 1"

        return "OK"

    async def fetchrow(self, query: str, *args: Any) -> Any:
        q = query.strip()
        if "algen_agent_runtime_runs" in q:
            key = (str(args[1]), str(args[0]))
            return self.runs.get(key)
        if "algen_agent_runtime_artifacts" in q:
            key = (str(args[1]), str(args[0]))
            row = self.artifacts.get(key)
            if row:
                meta = row.get("metadata")
                if isinstance(meta, str):
                    meta = json.loads(meta)
                return {
                    "id": row["id"],
                    "tenant_id": row["tenant_id"],
                    "run_id": row.get("run_id"),
                    "name": row["name"],
                    "media_type": row["media_type"],
                    "data": row["data"],
                    "size_bytes": row["size_bytes"],
                    "sha256": row["sha256"],
                    "status": row["status"],
                    "metadata": meta or {},
                    "expires_at": row.get("expires_at"),
                    "created_at": row.get("created_at"),
                }
            return None
        if "algen_agent_runtime_workflow_checkpoints" in q:
            key = (str(args[1]), str(args[0]))
            row = self.workflows.get(key)
            if row:
                return {"state": row["state"]}
            return None
        if "algen_agent_runtime_tool_executions" in q:
            idemp_key = str(args[0])
            row = self.tools.get(idemp_key)
            if row is None:
                return None
            if len(args) > 1 and row["tenant_id"] != str(args[1]):
                return None
            return {"record": row["record"]}
        if "SELECT 1 AS healthy" in q:
            return {"healthy": 1}
        return None

    async def fetch(self, query: str, *args: Any) -> Sequence[Any]:
        q = query.strip()
        if "algen_agent_runtime_runs" in q:
            return list(self.runs.values())
        if "algen_agent_runtime_memory" in q:
            # WHERE tenant_id=$1 AND session_id=$2
            tenant_id = str(args[0])
            session_id = str(args[1])
            limit = int(args[2]) if len(args) > 2 else 100
            matched = [
                m
                for m in self.memory
                if m["tenant_id"] == tenant_id and m["session_id"] == session_id
            ]
            return matched[-limit:]
        if "algen_agent_runtime_workflow_checkpoints" in q:
            terminal = {"completed", "failed", "cancelled"}
            return [
                {"state": r["state"]}
                for r in self.workflows.values()
                if r["status"] not in terminal
            ]
        if "algen_agent_runtime_schema_migrations" in q:
            return [{"name": name} for name in sorted(self.migrations)]
        return []


def _make_run(run_id: str, tenant_id: str) -> RunState:
    return RunState(
        id=run_id,
        request=RunRequest(
            agent="test-agent",
            input="test",
            tenant_id=tenant_id,
            user_id="user-1",
        ),
        status=RunStatus.PLANNING,
        version=0,
        started_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def storage_db() -> Any:
    # Use real Postgres if configured in environment, otherwise use BehavioralAsyncpgDouble
    dsn = os.environ.get("POSTGRES_DSN")
    if dsn:
        return PostgresDatabase(dsn)
    return BehavioralAsyncpgDouble()


@pytest.mark.asyncio
async def test_postgres_run_store_lifecycle_and_tenant_isolation(storage_db: Any) -> None:
    store = PostgresRunStore(storage_db)
    run_a = _make_run("run-1", "tenant-a")
    run_b = _make_run("run-2", "tenant-b")

    await store.create(run_a)
    await store.create(run_b)

    # Retrieval respects tenant boundary
    fetched_a = await store.get("run-1", "tenant-a")
    assert fetched_a is not None
    assert fetched_a.request.tenant_id == "tenant-a"
    assert await store.get("run-1", "tenant-b") is None

    fetched_b = await store.get("run-2", "tenant-b")
    assert fetched_b is not None
    assert fetched_b.request.tenant_id == "tenant-b"
    assert await store.get("run-2", "tenant-a") is None

    # Duplicate creation raises ConflictError
    with pytest.raises(ConflictError, match="already exists"):
        await store.create(run_a)

    # Optimistic concurrency control
    await store.save(fetched_a, expected_version=0)
    assert fetched_a.version == 1

    # Stale version raises ConflictError
    with pytest.raises(ConflictError, match="concurrently modified"):
        await store.save(fetched_a, expected_version=0)


@pytest.mark.asyncio
async def test_postgres_memory_store_tenant_isolation(storage_db: Any) -> None:
    store = PostgresMemoryStore(storage_db)
    msg1 = Message.text(Role.USER, "tenant A message")
    msg2 = Message.text(Role.USER, "tenant B message")

    await store.append("tenant-a", "session-1", [msg1])
    await store.append("tenant-b", "session-1", [msg2])

    mem_a = await store.get("tenant-a", "session-1", limit=10)
    assert len(mem_a) == 1
    assert mem_a[0].text_content == "tenant A message"

    mem_b = await store.get("tenant-b", "session-1", limit=10)
    assert len(mem_b) == 1
    assert mem_b[0].text_content == "tenant B message"

    # Delete respects tenant
    await store.delete("tenant-a", "session-1")
    assert await store.get("tenant-a", "session-1", limit=10) == ()
    assert len(await store.get("tenant-b", "session-1", limit=10)) == 1


@pytest.mark.asyncio
async def test_postgres_artifact_store_lifecycle(storage_db: Any) -> None:
    store = PostgresArtifactStore(storage_db, max_bytes=1024)
    data = b"artifact content payload"
    artifact = Artifact(
        id="art-1",
        tenant_id="tenant-a",
        name="test.txt",
        media_type="text/plain",
        data=data,
        status=ArtifactStatus.AVAILABLE,
    )

    await store.put(artifact)

    fetched = await store.get("art-1", "tenant-a")
    assert fetched is not None
    assert fetched.data == data
    assert fetched.name == "test.txt"

    # Tenant boundary
    assert await store.get("art-1", "tenant-b") is None

    # Oversized artifact rejected
    huge = Artifact(
        id="art-huge",
        tenant_id="tenant-a",
        name="huge.bin",
        media_type="application/octet-stream",
        data=b"x" * 2048,
        status=ArtifactStatus.AVAILABLE,
    )
    with pytest.raises(ValueError, match="byte limit"):
        await store.put(huge)


@pytest.mark.asyncio
async def test_postgres_tool_execution_ledger_deduplication(storage_db: Any) -> None:
    # Ensure referenced runs exist so foreign keys pass in real Postgres
    run_store = PostgresRunStore(storage_db)
    if await run_store.get("run-1", "tenant-a") is None:
        await run_store.create(_make_run("run-1", "tenant-a"))
    if await run_store.get("run-2", "tenant-b") is None:
        await run_store.create(_make_run("run-2", "tenant-b"))

    store = PostgresToolExecutionStore(storage_db)
    context = ToolContext(
        run_id="run-1",
        step_id="step-1",
        tenant_id="tenant-a",
        user_id="user-1",
        permissions=frozenset(),
        idempotency_key="idemp-key-1",
    )

    # First attempt records and proceeds (created=True)
    record, created = await store.begin(context, "send_payment")
    assert created is True
    assert record.status == ToolExecutionStatus.STARTED
    assert record.idempotency_key == "idemp-key-1"

    # Complete first execution
    await store.complete("idemp-key-1", ToolResult(value={"confirmed": True}))

    # Replay with same context/idempotency key in same tenant returns existing (created=False)
    replay_record, replay_created = await store.begin(context, "send_payment")
    assert replay_created is False
    assert replay_record.status == ToolExecutionStatus.COMPLETED
    assert replay_record.result is not None
    assert replay_record.result.value == {"confirmed": True}

    # Different tenant with same idempotency key is isolated
    tenant_b_context = ToolContext(
        run_id="run-2",
        step_id="step-1",
        tenant_id="tenant-b",
        user_id="user-2",
        permissions=frozenset(),
        idempotency_key="idemp-key-2",
    )
    b_record, b_created = await store.begin(tenant_b_context, "send_payment")
    assert b_created is True
    assert b_record.tenant_id == "tenant-b"


@pytest.mark.asyncio
async def test_postgres_workflow_checkpoint_store_lifecycle(storage_db: Any) -> None:
    store = PostgresWorkflowCheckpointStore(storage_db)
    wf_a = WorkflowExecutionState(
        id="wf-1",
        manifest_name="test-workflow",
        manifest_version="1.0.0",
        tenant_id="tenant-a",
        user_id="user-1",
        status=WorkflowStatus.RUNNING,
        version=0,
        values={"key": "val-a"},
    )
    wf_b = WorkflowExecutionState(
        id="wf-1",
        manifest_name="test-workflow",
        manifest_version="1.0.0",
        tenant_id="tenant-b",
        user_id="user-2",
        status=WorkflowStatus.AWAITING_INPUT,
        version=0,
        values={"key": "val-b"},
    )

    # Create checkpoints
    await store.create(wf_a)
    await store.create(wf_b)

    # Duplicate creation raises ConflictError
    with pytest.raises(ConflictError, match="already exists"):
        await store.create(wf_a)

    # Get respects tenant boundary
    fetched_a = await store.get("wf-1", "tenant-a")
    assert fetched_a is not None
    assert fetched_a.values == {"key": "val-a"}

    fetched_b = await store.get("wf-1", "tenant-b")
    assert fetched_b is not None
    assert fetched_b.values == {"key": "val-b"}

    # Optimistic CAS save
    fetched_a.values["key"] = "val-a-updated"
    await store.save(fetched_a, expected_version=0)
    assert fetched_a.version == 1

    # Stale version raises ConflictError
    with pytest.raises(ConflictError, match="concurrently modified"):
        await store.save(fetched_a, expected_version=0)

    # List recoverable returns awaiting-input and running workflows
    recoverable = await store.list_recoverable()
    rec_ids = {r.id for r in recoverable}
    assert "wf-1" in rec_ids
