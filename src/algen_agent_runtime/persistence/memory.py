from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactDescriptor,
    ArtifactStatus,
    Message,
    RunState,
    utc_now,
)


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
            if (
                artifact is None
                or artifact.tenant_id != tenant_id
                or (artifact.expires_at is not None and artifact.expires_at <= utc_now())
            ):
                return None
            return artifact.model_copy(deep=True)

    async def describe(self, artifact_id: str, tenant_id: str) -> ArtifactDescriptor | None:
        artifact = await self.get(artifact_id, tenant_id)
        return artifact.descriptor() if artifact else None

    async def list(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        status: ArtifactStatus | None = None,
        limit: int = 100,
    ) -> Sequence[ArtifactDescriptor]:
        async with self._lock:
            artifacts = [
                artifact
                for artifact in self._artifacts.values()
                if artifact.tenant_id == tenant_id
                and (artifact.expires_at is None or artifact.expires_at > utc_now())
                and (run_id is None or artifact.run_id == run_id)
                and (status is None or artifact.status == status)
            ]
            artifacts.sort(key=lambda artifact: artifact.created_at, reverse=True)
            return tuple(artifact.descriptor() for artifact in artifacts[:limit])

    async def set_status(
        self,
        artifact_id: str,
        tenant_id: str,
        status: ArtifactStatus,
        *,
        expected_status: ArtifactStatus | None = None,
    ) -> ArtifactDescriptor | None:
        async with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if (
                artifact is None
                or artifact.tenant_id != tenant_id
                or (artifact.expires_at is not None and artifact.expires_at <= utc_now())
                or (expected_status is not None and artifact.status is not expected_status)
            ):
                return None
            updated = artifact.model_copy(update={"status": status}, deep=True)
            self._artifacts[artifact_id] = updated
            return updated.descriptor()

    async def delete(self, artifact_id: str, tenant_id: str) -> bool:
        async with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None or artifact.tenant_id != tenant_id:
                return False
            del self._artifacts[artifact_id]
            return True

    async def purge_expired(self, *, limit: int = 1000) -> int:
        now = utc_now()
        async with self._lock:
            expired = [
                artifact_id
                for artifact_id, artifact in self._artifacts.items()
                if artifact.expires_at is not None and artifact.expires_at <= now
            ][:limit]
            for artifact_id in expired:
                del self._artifacts[artifact_id]
            return len(expired)
