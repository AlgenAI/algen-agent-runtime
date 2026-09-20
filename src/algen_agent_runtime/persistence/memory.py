from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import Artifact, Message, RunState


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = asyncio.Lock()

    async def create(self, state: RunState) -> None:
        async with self._lock:
            if state.id in self._runs:
                raise ConflictError(f"run {state.id!r} already exists")
            self._runs[state.id] = state.model_copy(deep=True)

    async def get(self, run_id: str, tenant_id: str) -> RunState | None:
        async with self._lock:
            state = self._runs.get(run_id)
            if state is None or state.request.tenant_id != tenant_id:
                return None
            return state.model_copy(deep=True)

    async def save(self, state: RunState, expected_version: int) -> None:
        async with self._lock:
            current = self._runs.get(state.id)
            if current is None or current.version != expected_version:
                raise ConflictError(f"run {state.id!r} was concurrently modified")
            saved = state.model_copy(deep=True)
            saved.version = expected_version + 1
            state.version = saved.version
            self._runs[state.id] = saved

    async def list_active(self, limit: int = 1000) -> Sequence[RunState]:
        from algen_agent_runtime.types.contracts import TERMINAL_STATUSES

        async with self._lock:
            active = [
                state.model_copy(deep=True)
                for state in self._runs.values()
                if state.status not in TERMINAL_STATUSES
            ]
        active.sort(key=lambda state: state.updated_at)
        return tuple(active[:limit])


class InMemoryMemoryStore:
    def __init__(self) -> None:
        self._messages: dict[tuple[str, str], list[Message]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        async with self._lock:
            self._messages[(tenant_id, session_id)].extend(deepcopy(list(messages)))

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        async with self._lock:
            return tuple(deepcopy(self._messages[(tenant_id, session_id)][-limit:]))

    async def delete(self, tenant_id: str, session_id: str) -> None:
        async with self._lock:
            self._messages.pop((tenant_id, session_id), None)


class InMemoryArtifactStore:
    def __init__(self, max_bytes: int = 10_485_760) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._max_bytes = max_bytes
        self._lock = asyncio.Lock()

    async def put(self, artifact: Artifact) -> None:
        if len(artifact.data) > self._max_bytes:
            raise ValueError(f"artifact exceeds {self._max_bytes} byte limit")
        async with self._lock:
            self._artifacts[artifact.id] = artifact.model_copy(deep=True)

    async def get(self, artifact_id: str, tenant_id: str) -> Artifact | None:
        async with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None or artifact.tenant_id != tenant_id:
                return None
            return artifact.model_copy(deep=True)
