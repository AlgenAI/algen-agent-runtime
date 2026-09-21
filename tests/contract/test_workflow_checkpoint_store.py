from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import utc_now
from algen_agent_runtime.workflows import (
    InMemoryWorkflowCheckpointStore,
    PostgresWorkflowCheckpointStore,
    WorkflowExecutionState,
    WorkflowStatus,
)


class FakeWorkflowDatabase:
    """Behavioral PostgreSQL double for store conformance and CAS failure injection."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def execute(self, statement: str, *args: Any) -> str:
        if statement.startswith("INSERT"):
            key = (str(args[1]), str(args[0]))
            if key in self.rows:
                raise ConflictError("duplicate workflow")
            self.rows[key] = {
                "state": str(args[6]),
                "status": str(args[4]),
                "version": int(args[5]),
                "updated_at": args[7],
            }
            return "INSERT 0 1"
        if statement.startswith("UPDATE"):
            key = (str(args[5]), str(args[4]))
            row = self.rows.get(key)
            if row is None or row["version"] != int(args[6]):
                return "UPDATE 0"
            row.update(
                state=str(args[2]),
                status=str(args[0]),
                version=int(args[1]),
                updated_at=args[3],
            )
            return "UPDATE 1"
        raise AssertionError(statement)

    async def fetchrow(self, statement: str, *args: Any) -> dict[str, Any] | None:
        row = self.rows.get((str(args[1]), str(args[0])))
        return {"state": row["state"]} if row else None

    async def fetch(self, statement: str, *args: Any) -> list[dict[str, Any]]:
        limit = int(args[0])
        terminal = {"completed", "failed", "cancelled"}
        rows = sorted(
            (row for row in self.rows.values() if row["status"] not in terminal),
            key=lambda row: row["updated_at"],
        )
        return [{"state": row["state"]} for row in rows[:limit]]


def state(
    workflow_id: str,
    *,
    tenant_id: str = "tenant-a",
    status: WorkflowStatus = WorkflowStatus.RUNNING,
) -> WorkflowExecutionState:
    return WorkflowExecutionState(
        id=workflow_id,
        manifest_name="contract-flow",
        manifest_version="1.0.0",
        tenant_id=tenant_id,
        user_id="operator",
        status=status,
        deadline_at=utc_now() + timedelta(minutes=5),
    )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_workflow_checkpoint_store_conformance(backend: str) -> None:
    store = (
        InMemoryWorkflowCheckpointStore()
        if backend == "memory"
        else PostgresWorkflowCheckpointStore(FakeWorkflowDatabase())
    )
    first = state("wf-1")
    terminal = state("wf-2", status=WorkflowStatus.COMPLETED)
    other_tenant = state("wf-3", tenant_id="tenant-b")
    await store.create(first)
    await store.create(terminal)
    await store.create(other_tenant)

    loaded = await store.get("wf-1", "tenant-a")
    assert loaded is not None
    loaded.values["changed"] = True
    untouched = await store.get("wf-1", "tenant-a")
    assert untouched is not None and "changed" not in untouched.values
    assert await store.get("wf-1", "tenant-b") is None

    loaded = await store.get("wf-1", "tenant-a")
    assert loaded is not None
    await store.save(loaded, loaded.version)
    assert loaded.version == 1
    stale = loaded.model_copy(update={"version": 0})
    with pytest.raises(ConflictError, match="concurrently modified"):
        await store.save(stale, 0)

    recoverable = await store.list_recoverable()
    assert {item.id for item in recoverable} == {"wf-1", "wf-3"}


@pytest.mark.asyncio
async def test_postgres_checkpoint_rejects_corrupt_persisted_state() -> None:
    database = FakeWorkflowDatabase()
    database.rows[("tenant-a", "wf-corrupt")] = {
        "state": json.dumps({"id": "wf-corrupt", "tenant_id": "tenant-a"}),
        "status": "running",
        "version": 0,
        "updated_at": utc_now(),
    }

    with pytest.raises(ValueError):
        await PostgresWorkflowCheckpointStore(database).get("wf-corrupt", "tenant-a")
