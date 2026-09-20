from __future__ import annotations

import asyncio
from typing import Any

from algen_agent_runtime.analytics.contracts import AnalyticalGraphState
from algen_agent_runtime.exceptions.errors import ConflictError


class InMemoryAnalyticalGraphStore:
    def __init__(self) -> None:
        self._states: dict[str, AnalyticalGraphState] = {}
        self._lock = asyncio.Lock()

    async def create(self, state: AnalyticalGraphState) -> None:
        async with self._lock:
            if state.id in self._states:
                raise ConflictError(f"analytical graph {state.id!r} already exists")
            self._states[state.id] = state.model_copy(deep=True)

    async def get(self, graph_id: str, tenant_id: str) -> AnalyticalGraphState | None:
        async with self._lock:
            state = self._states.get(graph_id)
            if state is None or state.tenant_id != tenant_id:
                return None
            return state.model_copy(deep=True)

    async def save(self, state: AnalyticalGraphState, expected_version: int) -> None:
        async with self._lock:
            current = self._states.get(state.id)
            if current is None or current.version != expected_version:
                raise ConflictError(f"analytical graph {state.id!r} was concurrently modified")
            state.version = expected_version + 1
            self._states[state.id] = state.model_copy(deep=True)


class PostgresAnalyticalGraphStore:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def create(self, state: AnalyticalGraphState) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_analytical_graphs "
            "(id, tenant_id, status, version, state) VALUES ($1,$2,$3,$4,$5::jsonb)",
            state.id,
            state.tenant_id,
            state.status.value,
            state.version,
            state.model_dump_json(by_alias=True),
        )

    async def get(self, graph_id: str, tenant_id: str) -> AnalyticalGraphState | None:
        row = await self._database.fetchrow(
            "SELECT state FROM algen_agent_runtime_analytical_graphs WHERE id=$1 AND tenant_id=$2",
            graph_id,
            tenant_id,
        )
        return AnalyticalGraphState.model_validate_json(row["state"]) if row else None

    async def save(self, state: AnalyticalGraphState, expected_version: int) -> None:
        state.version = expected_version + 1
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_analytical_graphs SET status=$1, version=$2, "
            "state=$3::jsonb, updated_at=now() WHERE id=$4 AND tenant_id=$5 AND version=$6",
            state.status.value,
            state.version,
            state.model_dump_json(by_alias=True),
            state.id,
            state.tenant_id,
            expected_version,
        )
        if result != "UPDATE 1":
            state.version = expected_version
            raise ConflictError(f"analytical graph {state.id!r} was concurrently modified")
