from __future__ import annotations

import asyncio
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Protocol

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.workflows.contracts import (
    WorkflowExecutionState,
    WorkflowStatus,
)


class WorkflowCheckpointStore(Protocol):
    async def create(self, state: WorkflowExecutionState) -> None: ...
    async def get(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState | None: ...
    async def save(self, state: WorkflowExecutionState, expected_version: int) -> None: ...
    async def list_recoverable(self, limit: int = 1000) -> Sequence[WorkflowExecutionState]: ...


class InMemoryWorkflowCheckpointStore:
    def __init__(self) -> None:
        self._states: dict[tuple[str, str], WorkflowExecutionState] = {}
        self._lock = asyncio.Lock()

    async def create(self, state: WorkflowExecutionState) -> None:
        async with self._lock:
            key = (state.tenant_id, state.id)
            if key in self._states:
                raise ConflictError(f"workflow {state.id!r} already exists")
            self._states[key] = state.model_copy(deep=True)

    async def get(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState | None:
        async with self._lock:
            state = self._states.get((tenant_id, workflow_id))
            if state is None:
                return None
            return deepcopy(state)

    async def save(self, state: WorkflowExecutionState, expected_version: int) -> None:
        async with self._lock:
            key = (state.tenant_id, state.id)
            current = self._states.get(key)
            if current is None or current.version != expected_version:
                raise ConflictError(f"workflow {state.id!r} was concurrently modified")
            saved = state.model_copy(update={"version": expected_version + 1}, deep=True)
            state.version = saved.version
            self._states[key] = saved

    async def list_recoverable(self, limit: int = 1000) -> Sequence[WorkflowExecutionState]:
        terminal = {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
        async with self._lock:
            states = [
                deepcopy(state) for state in self._states.values() if state.status not in terminal
            ]
        states.sort(key=lambda state: state.updated_at)
        return tuple(states[:limit])


class PostgresWorkflowCheckpointStore:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def create(self, state: WorkflowExecutionState) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_workflow_checkpoints "
            "(id, tenant_id, manifest_name, manifest_version, status, version, state, updated_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
            state.id,
            state.tenant_id,
            state.manifest_name,
            state.manifest_version,
            state.status.value,
            state.version,
            state.model_dump_json(),
            state.updated_at,
        )

    async def get(self, workflow_id: str, tenant_id: str) -> WorkflowExecutionState | None:
        row = await self._database.fetchrow(
            "SELECT state FROM algen_agent_runtime_workflow_checkpoints "
            "WHERE id=$1 AND tenant_id=$2",
            workflow_id,
            tenant_id,
        )
        return WorkflowExecutionState.model_validate_json(row["state"]) if row else None

    async def save(self, state: WorkflowExecutionState, expected_version: int) -> None:
        next_version = expected_version + 1
        saved = state.model_copy(update={"version": next_version})
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_workflow_checkpoints SET status=$1, version=$2, "
            "state=$3::jsonb, updated_at=$4 WHERE id=$5 AND tenant_id=$6 AND version=$7",
            saved.status.value,
            next_version,
            saved.model_dump_json(),
            saved.updated_at,
            saved.id,
            saved.tenant_id,
            expected_version,
        )
        if str(result) != "UPDATE 1":
            raise ConflictError(f"workflow {state.id!r} was concurrently modified")
        state.version = next_version

    async def list_recoverable(self, limit: int = 1000) -> Sequence[WorkflowExecutionState]:
        rows = await self._database.fetch(
            "SELECT state FROM algen_agent_runtime_workflow_checkpoints "
            "WHERE status NOT IN ('completed', 'failed', 'cancelled') "
            "ORDER BY updated_at ASC LIMIT $1",
            limit,
        )
        return tuple(WorkflowExecutionState.model_validate_json(row["state"]) for row in rows)
