from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog

from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    RunConflictError,
    StoreUnavailableError,
)
from algen_agent_runtime.persistence.naming import check_schema_name
from algen_agent_runtime.persistence.quotas import (
    ResourceUsage,
    StorageQuotas,
    postgres_quota_write,
    read_usage,
)
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

_logger = structlog.get_logger("algen_agent_runtime.tools.ledger")
_database_logger = structlog.get_logger("algen_agent_runtime.persistence.postgres")


# One key for every Runtime sharing the database: concurrent first starts create the schema and
# run the migrations one at a time instead of racing on CREATE SCHEMA and CREATE TABLE.
_MIGRATION_LOCK_KEY = 0x616C67656E5F7274  # "algen_rt"
# A migration may rewrite a large table: it is not held to the per-command timeout of store calls.
_MIGRATION_TIMEOUT_SECONDS = 24 * 3600.0
# SQLSTATE classes and codes that mean the server is unreachable or going away, not that the
# statement was wrong: connection exceptions (08), admin/crash shutdown and "cannot connect now"
# (57P01-57P03), and too many connections (53300).
_UNAVAILABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def _unavailable(exc: BaseException) -> StoreUnavailableError | None:
    """The runtime error for a driver error that means PostgreSQL is unavailable, else None.

    asyncpg is optional, so its errors are recognised by SQLSTATE rather than imported.
    ``TimeoutError`` is a command or connect timeout of the pool: a deadline set around a store
    call cancels it instead, so the caller's own timeout is never mistaken for one.
    """
    if isinstance(exc, StoreUnavailableError):
        return None
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str) and (
        sqlstate.startswith("08") or sqlstate in _UNAVAILABLE_SQLSTATES
    ):
        reason = type(exc).__name__
    elif isinstance(exc, TimeoutError):
        reason = "timed out"
    elif isinstance(exc, OSError):
        reason = type(exc).__name__
    else:
        return None
    return StoreUnavailableError(f"PostgreSQL is unavailable ({reason})")


class PostgresDatabase:
    """Lazily initialized asyncpg pool shared by all PostgreSQL stores.

    With ``schema``, every connection of the pool has ``search_path`` set to that schema alone,
    so the migrations and every store create and use their tables there. With
    ``initialize_schema`` the schema is created if it is missing.
    """

    def __init__(
        self,
        dsn: str,
        *,
        initialize_schema: bool = True,
        min_pool_size: int = 1,
        max_pool_size: int = 10,
        schema: str | None = None,
        connect_timeout_seconds: float = 10.0,
        command_timeout_seconds: float | None = 60.0,
    ) -> None:
        self._dsn = dsn
        self._initialize_schema = initialize_schema
        self._min_pool_size = min_pool_size
        self._max_pool_size = max_pool_size
        # Bounds on an unresponsive server: without them a call (and the cancellation of one)
        # waits on the network for minutes. See docs/operations.md#database-outages.
        self._connect_timeout = connect_timeout_seconds
        self._command_timeout = command_timeout_seconds
        self._schema = check_schema_name(schema) if schema is not None else None
        self._pool: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def schema(self) -> str | None:
        """The schema the Runtime's tables live in, or ``None`` for the server's search_path."""
        return self._schema

    async def start(self) -> None:
        if self._pool is not None:
            return
        try:
            await self._start()
        except Exception as exc:
            unavailable = _unavailable(exc)
            if unavailable is not None:
                raise unavailable from exc
            raise

    async def _start(self) -> None:
        async with self._lock:
            if self._pool is not None:
                return
            try:
                import asyncpg
            except ImportError as exc:
                raise ImportError("install algen-agent-runtime[postgres]") from exc
            options: dict[str, Any] = {}
            if self._schema is not None:
                # A startup parameter, so it is the session default that survives the RESET ALL
                # asyncpg runs when a connection returns to the pool. Only this schema: a table
                # missing from it must never resolve to another schema's table of the same name.
                options["server_settings"] = {"search_path": f'"{self._schema}"'}
            pool = await asyncpg.create_pool(
                self._dsn,
                min_size=self._min_pool_size,
                max_size=self._max_pool_size,
                timeout=self._connect_timeout,
                command_timeout=self._command_timeout,
                **options,
            )
            try:
                if self._initialize_schema:
                    async with pool.acquire() as connection:
                        await connection.fetchval(
                            "SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY
                        )
                        try:
                            if self._schema is not None:
                                await self._use_schema(connection, self._schema)
                            await self._migrate(connection)
                        finally:
                            await connection.fetchval(
                                "SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY
                            )
                elif self._schema is not None:
                    await self._use_schema(pool, self._schema)
            except BaseException:
                await pool.close()
                raise
            self._pool = pool

    async def _use_schema(self, pool: Any, schema: str) -> None:
        exists = await pool.fetchval(
            "SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname = $1", schema
        )
        if not exists:
            if not self._initialize_schema:
                raise ConfigurationError(
                    f"PostgreSQL schema {schema!r} does not exist and initialize_schema is false"
                )
            # The name was validated by check_schema_name and is quoted as an identifier.
            await pool.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        current = await pool.fetchval("SELECT current_schema()")
        if current is None:
            raise ConfigurationError(
                f"PostgreSQL schema {schema!r} exists but is not in effect: this role lacks USAGE "
                "on it, or the search_path startup parameter was dropped (a connection pooler may "
                "drop it)"
            )
        if current != schema:
            raise ConfigurationError(
                f"PostgreSQL connections use schema {current!r}, not postgres_schema {schema!r}: "
                "the search_path startup parameter was not applied (a connection pooler may "
                "drop it)"
            )
        if schema != "public" and await pool.fetchval(
            "SELECT to_regclass('algen_agent_runtime_schema_migrations') IS NULL "
            "AND to_regclass('public.algen_agent_runtime_schema_migrations') IS NOT NULL"
        ):
            # Tables left in public would otherwise vanish from the Runtime's view without a word.
            _database_logger.warning(
                "postgres_schema_is_empty_but_public_has_runtime_tables",
                postgres_schema=schema,
                legacy_schema="public",
                docs="docs/operations.md#sharing-a-database-postgres_schema",
            )

    async def _migrate(self, connection: Any) -> None:
        # Preserve the applied-migration history when upgrading from the former
        # runtime package name. This must happen before creating the new table so
        # existing installations do not replay the initial schema into empty,
        # newly named tables and leave their data behind.
        await connection.execute(
            "DO $$ BEGIN "
            "IF to_regclass('algen_agent_runtime_schema_migrations') IS NULL "
            "AND to_regclass('traccia_runtime_schema_migrations') IS NOT NULL THEN "
            "ALTER TABLE traccia_runtime_schema_migrations "
            "RENAME TO algen_agent_runtime_schema_migrations; "
            "END IF; END $$"
        )
        await connection.execute(
            "CREATE TABLE IF NOT EXISTS algen_agent_runtime_schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows = await connection.fetch("SELECT name FROM algen_agent_runtime_schema_migrations")
        applied = {str(row["name"]) for row in rows}
        directory = Path(__file__).with_name("migrations")
        for migration in sorted(directory.glob("*.sql")):
            if migration.name in applied:
                continue
            async with connection.transaction():
                await connection.execute(
                    migration.read_text(encoding="utf-8"), timeout=_MIGRATION_TIMEOUT_SECONDS
                )
                await connection.execute(
                    "INSERT INTO algen_agent_runtime_schema_migrations (name) VALUES ($1)",
                    migration.name,
                )

    @asynccontextmanager
    async def transaction(self) -> Any:
        await self.start()
        if self._pool is None:
            raise RuntimeError("PostgreSQL pool did not initialize")
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                yield connection
        except Exception as exc:
            raise await self._normalized(exc) from exc

    async def _pool_call(self, method: str, query: str, arguments: tuple[Any, ...]) -> Any:
        await self.start()
        pool = self._pool
        if pool is None:
            raise RuntimeError("PostgreSQL pool did not initialize")
        try:
            return await getattr(pool, method)(query, *arguments)
        except Exception as exc:
            raise await self._normalized(exc) from exc

    async def _normalized(self, exc: Exception) -> Exception:
        """``exc`` as the caller sees it: StoreUnavailableError when the server is unavailable.

        A lost server (a restart or failover) leaves every idle pooled connection dead, and
        asyncpg only finds out when one is used: the pool is told to replace them all, so one
        call fails rather than one per pooled connection.
        """
        unavailable = _unavailable(exc)
        if unavailable is None:
            return exc
        pool = self._pool
        if pool is not None:
            try:
                await pool.expire_connections()
            except Exception:  # the pool may be closing
                _database_logger.debug("postgres_expire_connections_failed", exc_info=True)
        return unavailable

    async def execute(self, query: str, *arguments: Any) -> str:
        return str(await self._pool_call("execute", query, arguments))

    async def fetchrow(self, query: str, *arguments: Any) -> Any:
        return await self._pool_call("fetchrow", query, arguments)

    async def fetch(self, query: str, *arguments: Any) -> Sequence[Any]:
        return tuple(await self._pool_call("fetch", query, arguments))

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
        try:
            await self._database.execute(
                "INSERT INTO algen_agent_runtime_runs (id, tenant_id, version, state) "
                "VALUES ($1, $2, $3, $4::json)",
                state.id,
                state.request.tenant_id,
                state.version,
                state.model_dump_json(),
            )
        except Exception as exc:
            if "UniqueViolationError" in type(exc).__name__:
                raise ConflictError(f"run {state.id!r} already exists") from exc
            raise

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
            "UPDATE algen_agent_runtime_runs SET version=$1, state=$2::json, updated_at=now() "
            "WHERE id=$3 AND tenant_id=$4 AND version=$5",
            next_version,
            saved.model_dump_json(),
            state.id,
            state.request.tenant_id,
            expected_version,
        )
        if result != "UPDATE 1":
            raise RunConflictError(f"run {state.id!r} was concurrently modified")
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
    def __init__(
        self,
        database: Any,
        retention_seconds: int = 86_400,
        quotas: StorageQuotas | None = None,
    ) -> None:
        self._database = database
        self._retention_seconds = retention_seconds
        self._quotas = quotas

    async def append(self, tenant_id: str, session_id: str, messages: Sequence[Message]) -> None:
        if not messages:
            return

        async def write(connection: Any) -> None:
            for message in messages:
                await connection.execute(
                    "INSERT INTO algen_agent_runtime_memory "
                    "(tenant_id, session_id, message, expires_at) "
                    "VALUES ($1, $2, $3::jsonb, now() + ($4 * interval '1 second'))",
                    tenant_id,
                    session_id,
                    message.model_dump_json(),
                    self._retention_seconds,
                )

        await postgres_quota_write(self._database, self._quotas, tenant_id, "memory", write)

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        return await read_usage(self._database, tenant_id, "memory")

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
    def __init__(
        self, database: Any, max_bytes: int = 10_485_760, quotas: StorageQuotas | None = None
    ) -> None:
        self._database = database
        self._max_bytes = max_bytes
        self._quotas = quotas

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        """Stored artifacts of every backend, expired ones included until they are purged."""
        return await read_usage(self._database, tenant_id, "artifacts")

    async def put(self, artifact: Artifact) -> None:
        if len(artifact.data) > self._max_bytes:
            raise ValueError(f"artifact exceeds {self._max_bytes} byte limit")
        await postgres_quota_write(
            self._database,
            self._quotas,
            artifact.tenant_id,
            "artifacts",
            lambda connection: self._insert(connection, artifact),
        )

    @staticmethod
    async def _insert(connection: Any, artifact: Artifact) -> None:
        descriptor = artifact.descriptor()
        await connection.execute(
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
        meta = payload.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta)
        payload["metadata"] = dict(meta or {})
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
        meta = payload.get("metadata")
        if isinstance(meta, str):
            meta = json.loads(meta)
        payload["metadata"] = dict(meta or {})
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
            # A retry keeps the decisions recorded about earlier attempts.
            record = record.model_copy(update={"resolutions": current.resolutions})
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

    async def get(self, idempotency_key: str, tenant_id: str) -> ToolExecutionRecord | None:
        row = await self._database.fetchrow(
            f"SELECT {_TOOL_RECORD_COLUMNS} FROM algen_agent_runtime_tool_executions "
            "WHERE idempotency_key=$1 AND tenant_id=$2",
            idempotency_key,
            tenant_id,
        )
        return _tool_record(row) if row else None

    async def find(
        self,
        tenant_id: str,
        *,
        statuses: Collection[ToolExecutionStatus],
        run_id: str | None = None,
        tool_name: str | None = None,
        min_age: timedelta | None = None,
        limit: int = 100,
    ) -> Sequence[ToolExecutionRecord]:
        rows = await self._database.fetch(
            f"SELECT {_TOOL_RECORD_COLUMNS} FROM algen_agent_runtime_tool_executions "
            "WHERE tenant_id=$1 AND status = ANY($2::text[]) "
            "AND ($3::text IS NULL OR run_id=$3) "
            "AND ($4::text IS NULL OR tool_name=$4) "
            "AND ($5::interval IS NULL OR updated_at <= now() - $5::interval) "
            "ORDER BY updated_at ASC LIMIT $6",
            tenant_id,
            sorted(status.value for status in statuses),
            run_id,
            tool_name,
            min_age,
            limit,
        )
        return tuple(_tool_record(row) for row in rows)

    async def transition(
        self, record: ToolExecutionRecord, *, expected: Collection[ToolExecutionStatus]
    ) -> ToolExecutionRecord | None:
        # Compare-and-set on status: of two concurrent decisions, exactly one matches.
        row = await self._database.fetchrow(
            "UPDATE algen_agent_runtime_tool_executions SET status=$1, record=$2::jsonb, "
            "updated_at=now() WHERE idempotency_key=$3 AND tenant_id=$4 "
            f"AND status = ANY($5::text[]) RETURNING {_TOOL_RECORD_COLUMNS}",
            record.status.value,
            record.model_dump_json(exclude={"created_at", "updated_at"}),
            record.idempotency_key,
            record.tenant_id,
            sorted(status.value for status in expected),
        )
        return _tool_record(row) if row else None

    async def _get(self, idempotency_key: str) -> ToolExecutionRecord:
        row = await self._database.fetchrow(
            "SELECT record FROM algen_agent_runtime_tool_executions WHERE idempotency_key=$1",
            idempotency_key,
        )
        if row is None:
            raise ConflictError("tool execution reservation was not found")
        return ToolExecutionRecord.model_validate_json(row["record"])

    async def _update(self, record: ToolExecutionRecord) -> None:
        # Only the call's own outcome for a reservation still in flight: a late outcome never
        # overwrites an operator's decision.
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_tool_executions SET status=$1, record=$2::jsonb, "
            "updated_at=now() WHERE idempotency_key=$3 AND status='started'",
            record.status.value,
            record.model_dump_json(),
            record.idempotency_key,
        )
        if result == "UPDATE 0":
            _logger.warning(
                "tool_execution_late_outcome_ignored",
                idempotency_key=record.idempotency_key,
                tool=record.tool_name,
                status=record.status.value,
            )


_TOOL_RECORD_COLUMNS = "record, created_at, updated_at"


def _tool_record(row: Any) -> ToolExecutionRecord:
    raw = row["record"]
    payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    # The columns, not the JSON copy, say when the row was written.
    payload["created_at"] = row["created_at"]
    payload["updated_at"] = row["updated_at"]
    return ToolExecutionRecord.model_validate(payload)
