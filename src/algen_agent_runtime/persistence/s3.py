from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Sequence
from typing import Any

from algen_agent_runtime.types.contracts import Artifact, ArtifactDescriptor, ArtifactStatus


class S3ArtifactStore:
    """S3-compatible blob storage with tenant-scoped PostgreSQL lifecycle metadata."""

    def __init__(
        self,
        database: Any,
        *,
        bucket: str,
        prefix: str = "algen-agent-runtime/artifacts",
        region: str | None = None,
        endpoint_url: str | None = None,
        addressing_style: str = "auto",
        server_side_encryption: str = "AES256",
        kms_key_id: str | None = None,
        max_bytes: int = 10_485_760,
        client: Any | None = None,
    ) -> None:
        self._database = database
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._max_bytes = max_bytes
        self._server_side_encryption = server_side_encryption
        self._kms_key_id = kms_key_id
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise ImportError("install algen-agent-runtime[object-storage]") from exc
            client = boto3.client(
                "s3",
                region_name=region,
                endpoint_url=endpoint_url,
                config=Config(s3={"addressing_style": addressing_style}),
            )
        self._client = client

    async def put(self, artifact: Artifact) -> None:
        if len(artifact.data) > self._max_bytes:
            raise ValueError(f"artifact exceeds {self._max_bytes} byte limit")
        descriptor = artifact.descriptor()
        key = self._object_key(artifact.tenant_id, artifact.id)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": artifact.data,
            "ContentType": artifact.media_type,
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(descriptor.sha256)).decode("ascii"),
            "ServerSideEncryption": self._server_side_encryption,
            "IfNoneMatch": "*",
            "Metadata": {
                "artifact-id": artifact.id,
                "tenant-hash": self._tenant_hash(artifact.tenant_id),
                "sha256": descriptor.sha256,
            },
        }
        if self._kms_key_id:
            kwargs["SSEKMSKeyId"] = self._kms_key_id
        await asyncio.to_thread(self._client.put_object, **kwargs)
        try:
            await self._database.execute(
                "INSERT INTO algen_agent_runtime_artifacts "
                "(id, tenant_id, run_id, name, media_type, data, size_bytes, sha256, status, "
                "metadata, expires_at, created_at, storage_backend, storage_key) "
                "VALUES ($1, $2, $3, $4, $5, NULL, $6, $7, $8, $9::jsonb, $10, $11, "
                "'s3', $12)",
                artifact.id,
                artifact.tenant_id,
                artifact.run_id,
                artifact.name,
                artifact.media_type,
                descriptor.size_bytes,
                descriptor.sha256,
                artifact.status.value,
                json.dumps(artifact.metadata),
                artifact.expires_at,
                artifact.created_at,
                key,
            )
        except Exception:
            await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=key)
            raise

    async def get(self, artifact_id: str, tenant_id: str) -> Artifact | None:
        row = await self._database.fetchrow(
            self._select(include_key=True)
            + " WHERE id=$1 AND tenant_id=$2 AND storage_backend='s3' "
            "AND (expires_at IS NULL OR expires_at > now())",
            artifact_id,
            tenant_id,
        )
        if not row:
            return None
        payload = dict(row)
        key = str(payload.pop("storage_key"))
        response = await asyncio.to_thread(
            self._client.get_object,
            Bucket=self._bucket,
            Key=key,
            ChecksumMode="ENABLED",
        )
        body = response["Body"]
        try:
            data = await asyncio.to_thread(body.read)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        payload["data"] = bytes(data)
        payload["metadata"] = dict(payload["metadata"] or {})
        return Artifact.model_validate(payload)

    async def describe(self, artifact_id: str, tenant_id: str) -> ArtifactDescriptor | None:
        row = await self._database.fetchrow(
            self._select() + " WHERE id=$1 AND tenant_id=$2 AND storage_backend='s3' "
            "AND (expires_at IS NULL OR expires_at > now())",
            artifact_id,
            tenant_id,
        )
        return self._descriptor(row) if row else None

    async def list(
        self,
        tenant_id: str,
        *,
        run_id: str | None = None,
        status: ArtifactStatus | None = None,
        limit: int = 100,
    ) -> Sequence[ArtifactDescriptor]:
        rows = await self._database.fetch(
            self._select() + " WHERE tenant_id=$1 AND storage_backend='s3' "
            "AND ($2::text IS NULL OR run_id=$2) "
            "AND ($3::text IS NULL OR status=$3) "
            "AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY created_at DESC LIMIT $4",
            tenant_id,
            run_id,
            status.value if status else None,
            limit,
        )
        return tuple(self._descriptor(row) for row in rows)

    async def set_status(
        self,
        artifact_id: str,
        tenant_id: str,
        status: ArtifactStatus,
        *,
        expected_status: ArtifactStatus | None = None,
    ) -> ArtifactDescriptor | None:
        row = await self._database.fetchrow(
            "UPDATE algen_agent_runtime_artifacts SET status=$3 "
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='s3' "
            "AND ($4::text IS NULL OR status=$4) "
            "AND (expires_at IS NULL OR expires_at > now()) RETURNING " + self._columns(),
            artifact_id,
            tenant_id,
            status.value,
            expected_status.value if expected_status else None,
        )
        return self._descriptor(row) if row else None

    async def delete(self, artifact_id: str, tenant_id: str) -> bool:
        row = await self._database.fetchrow(
            "SELECT storage_key FROM algen_agent_runtime_artifacts "
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='s3'",
            artifact_id,
            tenant_id,
        )
        if not row:
            return False
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=str(row["storage_key"]),
        )
        result = await self._database.execute(
            "DELETE FROM algen_agent_runtime_artifacts "
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='s3'",
            artifact_id,
            tenant_id,
        )
        return str(result) != "DELETE 0"

    async def purge_expired(self, *, limit: int = 1000) -> int:
        rows = await self._database.fetch(
            "SELECT id, tenant_id, storage_key FROM algen_agent_runtime_artifacts "
            "WHERE storage_backend='s3' AND expires_at IS NOT NULL AND expires_at <= now() "
            "ORDER BY expires_at LIMIT $1",
            limit,
        )
        deleted = 0
        for row in rows:
            if await self.delete(str(row["id"]), str(row["tenant_id"])):
                deleted += 1
        return deleted

    async def health(self) -> bool:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
            return bool(await self._database.health())
        except Exception:
            return False

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    def _object_key(self, tenant_id: str, artifact_id: str) -> str:
        return f"{self._prefix}/{self._tenant_hash(tenant_id)}/{artifact_id}"

    @staticmethod
    def _tenant_hash(tenant_id: str) -> str:
        return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _columns() -> str:
        return (
            "id, tenant_id, run_id, name, media_type, size_bytes, sha256, status, "
            "metadata, expires_at, created_at"
        )

    @classmethod
    def _select(cls, *, include_key: bool = False) -> str:
        suffix = ", storage_key" if include_key else ""
        return f"SELECT {cls._columns()}{suffix} FROM algen_agent_runtime_artifacts"

    @staticmethod
    def _descriptor(row: Any) -> ArtifactDescriptor:
        payload = dict(row)
        payload["metadata"] = dict(payload["metadata"] or {})
        return ArtifactDescriptor.model_validate(payload)
