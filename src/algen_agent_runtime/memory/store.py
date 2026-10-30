from __future__ import annotations

import asyncio
import json
from typing import Any

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.memory.contracts import (
    LearnedMemory,
    MemoryMutation,
    MemoryReceipt,
    MemoryScope,
)
from algen_agent_runtime.persistence.quotas import (
    ResourceUsage,
    StorageQuotas,
    lock_usage,
    read_usage,
)
from algen_agent_runtime.types.contracts import utc_now


def _scope(scope: MemoryScope) -> tuple[str, str, str]:
    return scope.tenant_id, scope.user_id, scope.agent_id


def _check_revision(current: LearnedMemory | None, mutation: MemoryMutation) -> None:
    if (current.revision if current else 0) != mutation.expected_revision:
        raise ConflictError("learned memory revision changed; recall the key before retrying")
    if not mutation.record.deleted and (
        mutation.record.expires_at is None or mutation.record.expires_at <= utc_now()
    ):
        raise ConflictError("learned memory write has expired")


def _usage_of(record: LearnedMemory | None) -> ResourceUsage:
    """What a record counts toward the learned-memory quota: only records holding content."""
    if record is None or record.deleted or record.content is None:
        return ResourceUsage()
    return ResourceUsage(1, len(record.model_dump_json().encode()))


def _receipt(mutation: MemoryMutation) -> MemoryReceipt:
    return MemoryReceipt(
        key=mutation.record.key, revision=mutation.record.revision, deleted=mutation.record.deleted
    )


class InMemoryLearnedMemoryStore:
    """Ephemeral adapter with the same CAS and replay semantics as PostgreSQL."""

    def __init__(self, quotas: StorageQuotas | None = None) -> None:
        self._records: dict[tuple[str, str, str, str], LearnedMemory] = {}
        self._operations: dict[tuple[str, str, str, str], tuple[str, MemoryReceipt]] = {}
        self._quotas = quotas
        self._lock = asyncio.Lock()

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        async with self._lock:
            return self._usage(tenant_id)

    def _usage(self, tenant_id: str) -> ResourceUsage:
        counted = [_usage_of(r) for key, r in self._records.items() if key[0] == tenant_id]
        return ResourceUsage(sum(c.items for c in counted), sum(c.bytes for c in counted))

    async def get(self, scope: MemoryScope, key: str) -> LearnedMemory | None:
        record = self._records.get((*_scope(scope), key))
        return record.model_copy(deep=True) if record else None

    async def search(self, scope: MemoryScope, query: str, limit: int) -> tuple[LearnedMemory, ...]:
        if not 1 <= limit <= 21:
            raise ValueError("memory search limit must be between 1 and 21")
        rows = [
            r
            for key, r in self._records.items()
            if key[:3] == _scope(scope)
            and not r.deleted
            and r.expires_at is not None
            and r.expires_at > utc_now()
            and query.lower() in (r.key + " " + (r.content or "")).lower()
        ]
        rows.sort(key=lambda r: (r.updated_at, r.key), reverse=True)
        return tuple(r.model_copy(deep=True) for r in rows[:limit])

    async def apply(self, mutation: MemoryMutation) -> MemoryReceipt:
        async with self._lock:
            operation = (*_scope(mutation.scope), mutation.operation_id)
            previous = self._operations.get(operation)
            if previous is not None:
                if previous[0] != mutation.fingerprint:
                    raise ConflictError(
                        "memory operation identity was reused with different content"
                    )
                return previous[1]
            key = (*_scope(mutation.scope), mutation.record.key)
            current = self._records.get(key)
            _check_revision(current, mutation)
            tenant_id = mutation.scope.tenant_id
            if self._quotas is not None and self._quotas.enforced(tenant_id, "learned_memory"):
                before = self._usage(tenant_id)
                old, new = _usage_of(current), _usage_of(mutation.record)
                after = ResourceUsage(
                    before.items - old.items + new.items, before.bytes - old.bytes + new.bytes
                )
                self._quotas.check(tenant_id, "learned_memory", before, after)
            receipt = _receipt(mutation)
            self._records[key] = mutation.record
            self._operations[operation] = (mutation.fingerprint, receipt)
            return receipt

    async def purge_expired(self, limit: int = 1000) -> int:
        if not 1 <= limit <= 10000:
            raise ValueError("invalid purge limit")
        async with self._lock:
            keys = [
                k
                for k, r in self._records.items()
                if r.expires_at is not None and r.expires_at <= utc_now() and r.content is not None
            ][:limit]
            for key in keys:
                self._records[key] = self._records[key].model_copy(
                    update={"content": None, "evidence": None}
                )
            return len(keys)


class PostgresLearnedMemoryStore:
    def __init__(self, database: Any, quotas: StorageQuotas | None = None) -> None:
        self._database = database
        self._quotas = quotas

    async def tenant_usage(self, tenant_id: str) -> ResourceUsage:
        return await read_usage(self._database, tenant_id, "learned_memory")

    @staticmethod
    def _decode(value: Any) -> LearnedMemory:
        return LearnedMemory.model_validate(json.loads(value) if isinstance(value, str) else value)

    async def get(self, scope: MemoryScope, key: str) -> LearnedMemory | None:
        row = await self._database.fetchrow(
            "SELECT record FROM algen_agent_runtime_learned_memory WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 AND key=$4",
            *_scope(scope),
            key,
        )
        return self._decode(row["record"]) if row else None

    async def search(self, scope: MemoryScope, query: str, limit: int) -> tuple[LearnedMemory, ...]:
        if not 1 <= limit <= 21:
            raise ValueError("memory search limit must be between 1 and 21")
        rows = await self._database.fetch(
            "SELECT record FROM algen_agent_runtime_learned_memory WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 "
            "AND NOT deleted AND expires_at > now() AND strpos(lower(key || ' ' || coalesce(record->>'content', '')), lower($4)) > 0 "
            "ORDER BY updated_at DESC, key DESC LIMIT $5",
            *_scope(scope),
            query,
            limit,
        )
        return tuple(self._decode(row["record"]) for row in rows)

    async def apply(self, mutation: MemoryMutation) -> MemoryReceipt:
        scope = _scope(mutation.scope)
        try:
            async with self._database.transaction() as connection:
                # A scope lock covers both first inserts and the replay ledger; row
                # locks alone cannot serialize writes to an absent memory key.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    mutation.scope.model_dump_json(),
                )
                previous = await connection.fetchrow(
                    "SELECT fingerprint, receipt FROM algen_agent_runtime_memory_operations WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 AND operation_id=$4",
                    *scope,
                    mutation.operation_id,
                )
                if previous is not None:
                    if previous["fingerprint"] != mutation.fingerprint:
                        raise ConflictError(
                            "memory operation identity was reused with different content"
                        )
                    value = previous["receipt"]
                    return MemoryReceipt.model_validate(
                        json.loads(value) if isinstance(value, str) else value
                    )
                row = await connection.fetchrow(
                    "SELECT record FROM algen_agent_runtime_learned_memory WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 AND key=$4 FOR UPDATE",
                    *scope,
                    mutation.record.key,
                )
                _check_revision(self._decode(row["record"]) if row else None, mutation)
                tenant_id = mutation.scope.tenant_id
                quotas = self._quotas
                checked = quotas is not None and quotas.enforced(tenant_id, "learned_memory")
                # Locked after the record row, in the order the expiry purge takes them.
                before = (
                    await lock_usage(connection, tenant_id, "learned_memory") if checked else None
                )
                record = mutation.record
                await connection.execute(
                    "INSERT INTO algen_agent_runtime_learned_memory (tenant_id,user_id,agent_id,key,revision,record,deleted,expires_at,updated_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9) ON CONFLICT (tenant_id,user_id,agent_id,key) DO UPDATE "
                    "SET revision=EXCLUDED.revision, record=EXCLUDED.record, deleted=EXCLUDED.deleted, expires_at=EXCLUDED.expires_at, updated_at=EXCLUDED.updated_at",
                    *scope,
                    record.key,
                    record.revision,
                    record.model_dump_json(),
                    record.deleted,
                    record.expires_at,
                    record.updated_at,
                )
                if quotas is not None and before is not None:
                    quotas.check(
                        tenant_id,
                        "learned_memory",
                        before,
                        await read_usage(connection, tenant_id, "learned_memory"),
                    )
                receipt = _receipt(mutation)
                await connection.execute(
                    "INSERT INTO algen_agent_runtime_memory_operations (tenant_id,user_id,agent_id,operation_id,fingerprint,receipt) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                    *scope,
                    mutation.operation_id,
                    mutation.fingerprint,
                    receipt.model_dump_json(),
                )
                return receipt
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                raise ConflictError("learned memory was concurrently modified") from exc
            raise

    async def purge_expired(self, limit: int = 1000) -> int:
        if not 1 <= limit <= 10000:
            raise ValueError("invalid purge limit")
        rows = await self._database.fetch(
            "WITH expired AS (SELECT tenant_id,user_id,agent_id,key FROM algen_agent_runtime_learned_memory "
            "WHERE expires_at <= now() AND record->>'content' IS NOT NULL LIMIT $1 FOR UPDATE SKIP LOCKED) "
            "UPDATE algen_agent_runtime_learned_memory AS m SET record=jsonb_set(jsonb_set(m.record, '{content}', 'null'::jsonb), '{evidence}', 'null'::jsonb) "
            "FROM expired e WHERE m.tenant_id=e.tenant_id AND m.user_id=e.user_id AND m.agent_id=e.agent_id AND m.key=e.key RETURNING m.key",
            limit,
        )
        return len(rows)
