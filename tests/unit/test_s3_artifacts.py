from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from algen_agent_runtime.artifacts import ArtifactLifecycleService, ArtifactScanResult
from algen_agent_runtime.events.bus import InMemoryAuditLog
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.persistence.s3 import S3ArtifactStore
from algen_agent_runtime.types.contracts import Artifact, ArtifactStatus


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def put_object(self, **kwargs: Any) -> None:
        self.put_calls.append(kwargs)
        self.objects[str(kwargs["Key"])] = bytes(kwargs["Body"])

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"Body": FakeBody(self.objects[str(kwargs["Key"])])}

    def delete_object(self, **kwargs: Any) -> None:
        key = str(kwargs["Key"])
        self.deleted.append(key)
        self.objects.pop(key, None)

    def head_bucket(self, **kwargs: Any) -> None:
        return None


async def test_s3_artifact_store_encrypts_checksums_and_hides_tenant_in_key() -> None:
    database = AsyncMock()
    client = FakeS3Client()
    store = S3ArtifactStore(
        database,
        bucket="runtime-artifacts",
        server_side_encryption="aws:kms",
        kms_key_id="alias/runtime-artifacts",
        client=client,
    )
    artifact = Artifact(
        tenant_id="private/customer/path",
        name="candidate.pdf",
        media_type="application/pdf",
        data=b"resume",
        status=ArtifactStatus.PENDING_SCAN,
    )

    await store.put(artifact)

    call = client.put_calls[0]
    assert call["ServerSideEncryption"] == "aws:kms"
    assert call["SSEKMSKeyId"] == "alias/runtime-artifacts"
    assert call["IfNoneMatch"] == "*"
    assert call["ChecksumSHA256"]
    assert artifact.tenant_id not in call["Key"]
    database.execute.assert_awaited_once()


async def test_s3_artifact_store_reads_and_verifies_blob_against_metadata() -> None:
    database = AsyncMock()
    client = FakeS3Client()
    data = b"verified-content"
    key = "artifacts/tenant-hash/artifact-id"
    client.objects[key] = data
    database.fetchrow.return_value = {
        "id": "artifact-id",
        "tenant_id": "tenant",
        "run_id": None,
        "name": "file.txt",
        "media_type": "text/plain",
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": "available",
        "metadata": {},
        "expires_at": None,
        "created_at": "2026-09-21T00:00:00Z",
        "storage_key": key,
    }
    store = S3ArtifactStore(database, bucket="runtime-artifacts", client=client)

    artifact = await store.get("artifact-id", "tenant")

    assert artifact is not None
    assert artifact.data == data
    assert artifact.descriptor().sha256 == hashlib.sha256(data).hexdigest()


async def test_s3_artifact_store_compensates_when_metadata_write_fails() -> None:
    database = AsyncMock()
    database.execute.side_effect = RuntimeError("database unavailable")
    client = FakeS3Client()
    store = S3ArtifactStore(database, bucket="runtime-artifacts", client=client)
    artifact = Artifact(
        tenant_id="tenant",
        name="file.txt",
        media_type="text/plain",
        data=b"content",
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await store.put(artifact)

    assert len(client.deleted) == 1
    assert client.objects == {}


class CleanScanner:
    async def scan(self, artifact: Artifact) -> ArtifactScanResult:
        assert artifact.data == b"safe"
        return ArtifactScanResult(
            status=ArtifactStatus.AVAILABLE,
            scanner="test-scanner",
            scanner_version="1.0",
            reason_code="clean",
        )


async def test_artifact_scanner_transition_is_audited() -> None:
    store = InMemoryArtifactStore()
    audit = InMemoryAuditLog()
    artifact = Artifact(
        tenant_id="tenant",
        name="file.txt",
        media_type="text/plain",
        data=b"safe",
        status=ArtifactStatus.PENDING_SCAN,
    )
    await store.put(artifact)

    updated = await ArtifactLifecycleService(store, audit).scan(
        artifact.id, "tenant", "scanner-worker", CleanScanner()
    )

    assert updated.status is ArtifactStatus.AVAILABLE
    events = await audit.list("tenant", artifact.id)
    assert len(events) == 1
    assert events[0].action == "artifact.scan"
    assert events[0].metadata["sha256"] == updated.sha256

    with pytest.raises(ConflictError, match="expected pending_scan"):
        await ArtifactLifecycleService(store, audit).scan(
            artifact.id, "tenant", "scanner-worker", CleanScanner()
        )


class RacingScanner:
    def __init__(self, store: InMemoryArtifactStore, artifact_id: str) -> None:
        self._store = store
        self._artifact_id = artifact_id

    async def scan(self, artifact: Artifact) -> ArtifactScanResult:
        await self._store.set_status(
            self._artifact_id,
            artifact.tenant_id,
            ArtifactStatus.QUARANTINED,
            expected_status=ArtifactStatus.PENDING_SCAN,
        )
        return ArtifactScanResult(status=ArtifactStatus.AVAILABLE, scanner="late-scanner")


async def test_artifact_scanner_cannot_overwrite_a_concurrent_transition() -> None:
    store = InMemoryArtifactStore()
    audit = InMemoryAuditLog()
    artifact = Artifact(
        tenant_id="tenant",
        name="file.txt",
        media_type="text/plain",
        data=b"unsafe",
        status=ArtifactStatus.PENDING_SCAN,
    )
    await store.put(artifact)

    with pytest.raises(ConflictError, match="changed while scan completed"):
        await ArtifactLifecycleService(store, audit).scan(
            artifact.id,
            "tenant",
            "scanner-worker",
            RacingScanner(store, artifact.id),
        )

    current = await store.describe(artifact.id, "tenant")
    assert current is not None
    assert current.status is ArtifactStatus.QUARANTINED
    assert await audit.list("tenant", artifact.id) == ()
