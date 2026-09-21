from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.tools.contracts import (
    ToolContext,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.types.contracts import (
    Artifact,
    ArtifactDescriptor,
    ArtifactStatus,
    Message,
    RunState,
)


class PostgresDatabase:
    """Lazily initialized asyncpg pool shared by all PostgreSQL stores."""

    def __init__(
        self,
        dsn: str,
        *,
        initialize_schema: bool = True,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._initialize_schema = initialize_schema
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        self._pool: Any | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._pool is not None:
            return
        async with self._lock:
            if self._pool is not None:
                return
            try:
                import asyncpg
            except ImportError as exc:
                raise ImportError("install algen-agent-runtime[postgres]") from exc
            pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_pool_size,
                max_size=self._max_pool_size,
            )
            self._pool = pool
            if self._initialize_schema:
                await self._migrate(pool)

    async def _migrate(self, pool: Any) -> None:
        # Preserve the applied-migration history when upgrading from the former
        # runtime package name. This must happen before creating the new table so
        # existing installations do not replay the initial schema into empty,
        # newly named tables and leave their data behind.
        await pool.execute(
            "DO $$ BEGIN "
            "IF to_regclass('algen_agent_runtime_schema_migrations') IS NULL "
            "AND to_regclass('traccia_runtime_schema_migrations') IS NOT NULL THEN "
            "ALTER TABLE traccia_runtime_schema_migrations "
            "RENAME TO algen_agent_runtime_schema_migrations; "
            "END IF; END $$"
        )
        await pool.execute(
            "CREATE TABLE IF NOT EXISTS algen_agent_runtime_schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows = await pool.fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
        applied = {str(row["name"]) for row in rows}
        directory = Path(__file__).with_name("migrations")
        for migration in sorted(directory.glob("*.sql")):
            if migration.name in applied:
                continue
            async with pool.acquire() as connection, connection.transaction():
                await connection.execute(migration.read_text(encoding="utf-8"))
                await connection.execute(
                    "INSERT INTO algen_agent_runtime_schema_migrations (name) VALUES ($1)",
                    migration.name,
                )

    async def execute(self, query: str, *arguments: Any) -> str:
        await self.start()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgreSQL pool did not initialize")
        return str(await pool.execute(query, *arguments))

    async def fetchrow(self, query: str, *arguments: Any) -> Any:
        await self.start()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgreSQL pool did not initialize")
        return await pool.fetchrow(query, *arguments)

    async def fetch(self, query: str, *arguments: Any) -> Sequence[Any]:
        await self.start()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgreSQL pool did not initialize")
        return tuple(await pool.fetch(query, *arguments))

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def health(self) -> bool:
        try:
            row = await self.fetchrow("SELECT 1 AS healthy")
            return bool(row and row["healthy"] == 1)
        except Exception:
            return False


class PostgresRunStore:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def create(self, state: RunState) -> None:
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_runs (id, tenant_id, version, state) "
            "VALUES ($1, $2, $3, $4::jsonb)",
            state.id,
            state.request.tenant_id,
            state.version,
            state.model_dump_json(),
        )

    async def get(self, run_id: str, tenant_id: str) -> RunState | None:
        row = await self._database.fetchrow(
            "SELECT state FROM algen_agent_runtime_runs WHERE id=$1 AND tenant_id=$2",
            run_id,
            tenant_id,
        )
        return RunState.model_validate_json(row["state"]) if row else None

    async def save(self, state: RunState, expected_version: int) -> None:
        next_version = expected_version + 1
        saved = state.model_copy(update={"version": next_version})
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_runs SET version=$1, state=$2::jsonb, updated_at=now() "
            "WHERE id=$3 AND tenant_id=$4 AND version=$5",
            next_version,
            saved.model_dump_json(),
            state.id,
            state.request.tenant_id,
            expected_version,
        )
        if result != "UPDATE 1":
            raise ConflictError(f"run {state.id!r} was concurrently modified")
        state.version = next_version

    async def list_active(self, limit: int = 1000) -> Sequence[RunState]:
        rows = await self._database.fetch(
            "SELECT state FROM algen_agent_runtime_runs "
            "WHERE state->>'status' NOT IN ('completed','failed','cancelled','timed_out') "
            "ORDER BY updated_at ASC LIMIT $1",
            limit,
        )
        return tuple(RunState.model_validate_json(row["state"]) for row in rows)


class PostgresMemoryStore:
    def __init__(self, database: Any, retention_seconds: int = 86_400) -> None:
        self._database = database
        self._retention_seconds = retention_seconds

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        for message in messages:
            await self._database.execute(
                "INSERT INTO algen_agent_runtime_memory "
                "(tenant_id, session_id, message, expires_at) "
                "VALUES ($1, $2, $3::jsonb, now() + ($4 * interval '1 second'))",
                tenant_id,
                session_id,
                message.model_dump_json(),
                self._retention_seconds,
            )

    async def get(self, tenant_id: str, session_id: str, limit: int) -> Sequence[Message]:
        rows = await self._database.fetch(
            "SELECT message FROM algen_agent_runtime_memory "
            "WHERE tenant_id=$1 AND session_id=$2 "
            "AND (expires_at IS NULL OR expires_at > now()) ORDER BY id DESC LIMIT $3",
            tenant_id,
            session_id,
            limit,
        )
        return tuple(Message.model_validate_json(row["message"]) for row in reversed(rows))

    async def delete(self, tenant_id: str, session_id: str) -> None:
        await self._database.execute(
            "DELETE FROM algen_agent_runtime_memory WHERE tenant_id=$1 AND session_id=$2",
            tenant_id,
            session_id,
        )


class PostgresArtifactStore:
    def __init__(self, database: Any, max_bytes: int = 10_485_760) -> None:
        self._database = database
        self._max_bytes = max_bytes

    async def put(self, artifact: Artifact) -> None:
        if len(artifact.data) > self._max_bytes:
            raise ValueError(f"artifact exceeds {self._max_bytes} byte limit")
        descriptor = artifact.descriptor()
        await self._database.execute(
            "INSERT INTO algen_agent_runtime_artifacts "
            "(id, tenant_id, run_id, name, media_type, data, size_bytes, sha256, status, "
            "metadata, expires_at, created_at, storage_backend, storage_key) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12, "
            "'postgres', NULL)",
            artifact.id,
            artifact.tenant_id,
            artifact.run_id,
            artifact.name,
            artifact.media_type,
            artifact.data,
            descriptor.size_bytes,
            descriptor.sha256,
            artifact.status.value,
            json.dumps(artifact.metadata),
            artifact.expires_at,
            artifact.created_at,
        )

    async def get(self, artifact_id: str, tenant_id: str) -> Artifact | None:
        row = await self._database.fetchrow(
            "SELECT id, tenant_id, run_id, name, media_type, data, size_bytes, sha256, status, "
            "metadata, expires_at, created_at FROM algen_agent_runtime_artifacts "
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='postgres' "
            "AND (expires_at IS NULL OR expires_at > now())",
            artifact_id,
            tenant_id,
        )
        if not row:
            return None
        payload = dict(row)
        payload["metadata"] = dict(payload["metadata"] or {})
        return Artifact.model_validate(payload)

    async def describe(self, artifact_id: str, tenant_id: str) -> ArtifactDescriptor | None:
        row = await self._database.fetchrow(
            "SELECT id, tenant_id, run_id, name, media_type, size_bytes, sha256, status, "
            "metadata, expires_at, created_at FROM algen_agent_runtime_artifacts "
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='postgres' "
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
            "SELECT id, tenant_id, run_id, name, media_type, size_bytes, sha256, status, "
            "metadata, expires_at, created_at FROM algen_agent_runtime_artifacts "
            "WHERE tenant_id=$1 AND storage_backend='postgres' "
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
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='postgres' "
            "AND ($4::text IS NULL OR status=$4) "
            "AND (expires_at IS NULL OR expires_at > now()) "
            "RETURNING id, tenant_id, run_id, name, media_type, size_bytes, sha256, status, "
            "metadata, expires_at, created_at",
            artifact_id,
            tenant_id,
            status.value,
            expected_status.value if expected_status else None,
        )
        return self._descriptor(row) if row else None

    async def delete(self, artifact_id: str, tenant_id: str) -> bool:
        result = await self._database.execute(
            "DELETE FROM algen_agent_runtime_artifacts "
            "WHERE id=$1 AND tenant_id=$2 AND storage_backend='postgres'",
            artifact_id,
            tenant_id,
        )
        return str(result) != "DELETE 0"

    async def purge_expired(self, *, limit: int = 1000) -> int:
        result = await self._database.execute(
            "DELETE FROM algen_agent_runtime_artifacts WHERE id IN "
            "(SELECT id FROM algen_agent_runtime_artifacts "
            "WHERE storage_backend='postgres' "
            "AND expires_at IS NOT NULL AND expires_at <= now() "
            "ORDER BY expires_at LIMIT $1)",
            limit,
        )
        return int(result.rsplit(" ", 1)[-1])

    @staticmethod
    def _descriptor(row: Any) -> ArtifactDescriptor:
        payload = dict(row)
        payload["metadata"] = dict(payload["metadata"] or {})
        return ArtifactDescriptor.model_validate(payload)


class PostgresToolExecutionStore:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def begin(self, context: ToolContext, tool_name: str) -> tuple[ToolExecutionRecord, bool]:
        record = ToolExecutionRecord(
            idempotency_key=context.idempotency_key,
            run_id=context.run_id,
            step_id=context.step_id,
            tenant_id=context.tenant_id,
            tool_name=tool_name,
            status=ToolExecutionStatus.STARTED,
        )
        result = await self._database.execute(
            "INSERT INTO algen_agent_runtime_tool_executions "
            "(idempotency_key, run_id, step_id, tenant_id, tool_name, status, record) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING",
            context.idempotency_key,
            context.run_id,
            context.step_id,
            context.tenant_id,
            tool_name,
            record.status.value,
            record.model_dump_json(),
        )
        if result == "INSERT 0 1":
            return record, True
        row = await self._database.fetchrow(
            "SELECT record FROM algen_agent_runtime_tool_executions "
            "WHERE idempotency_key=$1 AND tenant_id=$2",
            context.idempotency_key,
            context.tenant_id,
        )
        if row is None:
            raise ConflictError("tool execution record is not accessible to this tenant")
        current = ToolExecutionRecord.model_validate_json(row["record"])
        if current.status == ToolExecutionStatus.FAILED:
            changed = await self._database.execute(
                "UPDATE algen_agent_runtime_tool_executions SET status='started', record=$1::jsonb, "
                "updated_at=now() WHERE idempotency_key=$2 AND status='failed'",
                record.model_dump_json(),
                context.idempotency_key,
            )
            if changed == "UPDATE 1":
                return record, True
        return current, False

    async def complete(self, idempotency_key: str, result: ToolResult) -> None:
        current = await self._get(idempotency_key)
        updated = current.model_copy(
            update={"status": ToolExecutionStatus.COMPLETED, "result": result, "error": None}
        )
        await self._update(updated)

    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None:
        current = await self._get(idempotency_key)
        status = ToolExecutionStatus.INDETERMINATE if indeterminate else ToolExecutionStatus.FAILED
        await self._update(current.model_copy(update={"status": status, "error": error}))

    async def _get(self, idempotency_key: str) -> ToolExecutionRecord:
        row = await self._database.fetchrow(
            "SELECT record FROM algen_agent_runtime_tool_executions WHERE idempotency_key=$1",
            idempotency_key,
        )
        if row is None:
            raise ConflictError("tool execution reservation was not found")
        return ToolExecutionRecord.model_validate_json(row["record"])

    async def _update(self, record: ToolExecutionRecord) -> None:
        await self._database.execute(
            "UPDATE algen_agent_runtime_tool_executions SET status=$1, record=$2::jsonb, "
            "updated_at=now() WHERE idempotency_key=$3",
            record.status.value,
            record.model_dump_json(),
            record.idempotency_key,
        )
