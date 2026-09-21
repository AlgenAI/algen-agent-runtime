from __future__ import annotations

from typing import Protocol

from pydantic import Field, model_validator

from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactDescriptor,
    ArtifactStatus,
    StrictModel,
)
from algen_agent_runtime.types.interfaces import ArtifactStore, AuditLog


class ArtifactScanResult(StrictModel):
    """Provider-neutral, secret-free result from a deployment-owned content scanner."""

    status: ArtifactStatus
    scanner: str = Field(min_length=1, max_length=128)
    scanner_version: str | None = Field(default=None, max_length=64)
    reason_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_terminal_scan_status(self) -> ArtifactScanResult:
        if self.status is ArtifactStatus.PENDING_SCAN:
            raise ValueError("scanner must return available or quarantined")
        return self


class ArtifactScanner(Protocol):
    async def scan(self, artifact: Artifact) -> ArtifactScanResult: ...


class ArtifactLifecycleService:
    """Coordinates scanner decisions with artifact state and immutable audit evidence."""

    def __init__(self, store: ArtifactStore, audit: AuditLog) -> None:
        self._store = store
        self._audit = audit

    async def scan(
        self,
        artifact_id: str,
        tenant_id: str,
        actor_id: str,
        scanner: ArtifactScanner,
    ) -> ArtifactDescriptor:
        artifact = await self._store.get(artifact_id, tenant_id)
        if artifact is None:
            raise NotFoundError(f"artifact {artifact_id!r} not found")
        if artifact.status is not ArtifactStatus.PENDING_SCAN:
            raise ConflictError(
                f"artifact {artifact_id!r} is {artifact.status.value}, expected pending_scan"
            )
        result = await scanner.scan(artifact)
        updated = await self._store.set_status(
            artifact_id,
            tenant_id,
            result.status,
            expected_status=ArtifactStatus.PENDING_SCAN,
        )
        if updated is None:
            raise ConflictError(f"artifact {artifact_id!r} changed while scan completed")
        await self._audit.append(
            AuditEvent(
                action="artifact.scan",
                outcome=result.status.value,
                tenant_id=tenant_id,
                actor_id=actor_id,
                resource_id=artifact_id,
                metadata={
                    "scanner": result.scanner,
                    "scanner_version": result.scanner_version,
                    "reason_code": result.reason_code,
                    "sha256": updated.sha256,
                    "size_bytes": updated.size_bytes,
                },
            )
        )
        return updated
