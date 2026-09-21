from datetime import timedelta

import pytest
from pydantic import ValidationError

from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.types.contracts import Artifact, ArtifactStatus, utc_now


async def test_artifact_store_supports_lifecycle_listing_and_deletion() -> None:
    store = InMemoryArtifactStore()
    artifact = Artifact(
        tenant_id="tenant-a",
        name="candidate.pdf",
        media_type="application/pdf",
        data=b"resume",
        status=ArtifactStatus.PENDING_SCAN,
        metadata={"purpose": "candidate_resume"},
    )

    await store.put(artifact)
    descriptor = await store.describe(artifact.id, "tenant-a")

    assert descriptor is not None
    assert descriptor.size_bytes == 6
    assert len(descriptor.sha256) == 64
    assert descriptor.run_id is None
    assert await store.describe(artifact.id, "other-tenant") is None
    assert [item.id for item in await store.list("tenant-a")] == [artifact.id]

    available = await store.set_status(artifact.id, "tenant-a", ArtifactStatus.AVAILABLE)
    assert available is not None
    assert available.status is ArtifactStatus.AVAILABLE
    assert await store.delete(artifact.id, "other-tenant") is False
    assert await store.delete(artifact.id, "tenant-a") is True
    assert await store.get(artifact.id, "tenant-a") is None


async def test_artifact_store_purges_expired_artifacts() -> None:
    store = InMemoryArtifactStore()
    expired = Artifact(
        tenant_id="tenant",
        name="expired.txt",
        media_type="text/plain",
        data=b"expired",
        expires_at=utc_now() - timedelta(seconds=1),
    )
    active = Artifact(
        tenant_id="tenant",
        name="active.txt",
        media_type="text/plain",
        data=b"active",
        expires_at=utc_now() + timedelta(hours=1),
    )
    await store.put(expired)
    await store.put(active)

    assert await store.purge_expired() == 1
    assert await store.get(expired.id, "tenant") is None
    assert await store.get(active.id, "tenant") is not None


def test_artifact_rejects_false_integrity_metadata() -> None:
    with pytest.raises(ValidationError, match="size_bytes does not match"):
        Artifact(
            tenant_id="tenant",
            name="file.txt",
            media_type="text/plain",
            data=b"payload",
            size_bytes=1,
        )
