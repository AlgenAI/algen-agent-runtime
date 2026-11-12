from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Sequence
from copy import deepcopy

from algen_agent_runtime.exceptions.errors import ConflictError, RunConflictError
from algen_agent_runtime.persistence.quotas import ResourceUsage, StorageQuotas
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
                raise RunConflictError(f"run {state.id!r} was concurrently modified")
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

    async def find_by_turn(
        self, tenant_id: str, conversation_id: str, turn_id: str
    ) -> RunState | None:
        """The turn's most recently updated run, ended or not, or None."""
        async with self._lock:
            matches = [
                state
                for state in self._runs.values()
                if state.request.tenant_id == tenant_id
                and state.request.conversation_id == conversation_id
                and state.request.turn_id == turn_id
            ]
        if not matches:
            return None
        return max(matches, key=lambda state: state.updated_at).model_copy(deep=True)

    async def list_for_user(self, tenant_id: str, user_id: str) -> Sequence[RunState]:
        """Every run of ``user_id`` in the tenant, oldest first (for user data export/erasure)."""
        async with self._lock:
            runs = [
                state.model_copy(deep=True)
                for state in self._runs.values()
                if state.request.tenant_id == tenant_id and state.request.user_id == user_id
            ]
        runs.sort(key=lambda state: state.started_at)
        return tuple(runs)

    async def delete_many(self, tenant_id: str, run_ids: Sequence[str]) -> dict[str, int]:
        """Delete the tenant's runs among ``run_ids``; return ``{"runs": count}``."""
        async with self._lock:
            doomed = [
                run_id
                for run_id in dict.fromkeys(run_ids)
                if (state := self._runs.get(run_id)) is not None
                and state.request.tenant_id == tenant_id
            ]
            for run_id in doomed:
                del self._runs[run_id]
        return {"runs": len(doomed)}


def _message_bytes(message: Message) -> int:
    return len(message.model_dump_json().encode())


class InMemoryMemoryStore:
    def __init__(self, quotas: StorageQuotas | None = None) -> None:
        self._messages: dict[tuple[str, str], list[Message]] = defaultdict(list)
        # Each session's owner (the user who first used it), by tenant and session.
        self._owners: dict[tuple[str, str], str] = {}
        self._quotas = quotas
        self._lock = asyncio.Lock()

    async def claim_session(self, tenant_id: str, session_id: str, user_id: str) -> bool:
        """Bind the session to ``user_id`` on first use; False when another user owns it."""
        async with self._lock:
            return self._owners.setdefault((tenant_id, session_id), user_id) == user_id

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        async with self._lock:
            if self._quotas is not None and self._quotas.enforced(tenant_id, "memory"):
                before = self._usage(tenant_id)
                after = ResourceUsage(
                    before.items + len(messages),
                    before.bytes + sum(_message_bytes(message) for message in messages),
                )
                self._quotas.check(tenant_id, "memory", before, after)
            self._messages[(tenant_id, session_id)].extend(deepcopy(list(messages)))

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        async with self._lock:
            return self._usage(tenant_id)

    def _usage(self, tenant_id: str) -> ResourceUsage:
        stored = [
            message
            for (tenant, _), messages in self._messages.items()
            if tenant == tenant_id
            for message in messages
        ]
        return ResourceUsage(len(stored), sum(_message_bytes(message) for message in stored))

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        async with self._lock:
            return tuple(deepcopy(self._messages[(tenant_id, session_id)][-limit:]))

    async def delete(self, tenant_id: str, session_id: str) -> None:
        async with self._lock:
            self._messages.pop((tenant_id, session_id), None)

    async def delete_session(self, tenant_id: str, session_id: str) -> int:
        """Delete the session's memory and its owner; return how many messages went."""
        async with self._lock:
            self._owners.pop((tenant_id, session_id), None)
            return len(self._messages.pop((tenant_id, session_id), ()))

    async def session_owner(self, tenant_id: str, session_id: str) -> str | None:
        async with self._lock:
            return self._owners.get((tenant_id, session_id))

    async def sessions_for_user(self, tenant_id: str, user_id: str) -> Sequence[str]:
        """The sessions ``user_id`` owns in the tenant."""
        async with self._lock:
            return tuple(
                sorted(
                    session
                    for (tenant, session), owner in self._owners.items()
                    if tenant == tenant_id and owner == user_id
                )
            )


class InMemoryArtifactStore:
    def __init__(self, max_bytes: int = 10_485_760, quotas: StorageQuotas | None = None) -> None:
        self._artifacts: dict[str, Artifact] = {}
        self._max_bytes = max_bytes
        self._quotas = quotas
        self._lock = asyncio.Lock()

    async def put(self, artifact: Artifact) -> None:
        if len(artifact.data) > self._max_bytes:
            raise ValueError(f"artifact exceeds {self._max_bytes} byte limit")
        async with self._lock:
            tenant_id = artifact.tenant_id
            if self._quotas is not None and self._quotas.enforced(tenant_id, "artifacts"):
                before = self._usage(tenant_id)
                replaced = self._artifacts.get(artifact.id)
                after = ResourceUsage(before.items + 1, before.bytes + len(artifact.data))
                if replaced is not None and replaced.tenant_id == tenant_id:
                    after = ResourceUsage(after.items - 1, after.bytes - len(replaced.data))
                self._quotas.check(tenant_id, "artifacts", before, after)
            self._artifacts[artifact.id] = artifact.model_copy(deep=True)

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        """Stored artifacts, expired ones included until they are purged."""
        async with self._lock:
            return self._usage(tenant_id)

    def _usage(self, tenant_id: str) -> ResourceUsage:
        sizes = [
            len(artifact.data)
            for artifact in self._artifacts.values()
            if artifact.tenant_id == tenant_id
        ]
        return ResourceUsage(len(sizes), sum(sizes))

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
