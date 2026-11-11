from __future__ import annotations

import asyncio
import json
from typing import Any

from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.memory.contracts import (
    LearnedMemory,
    MemoryEmbedding,
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


def _active(record: LearnedMemory) -> bool:
    return (
        not record.deleted
        and record.content is not None
        and record.expires_at is not None
        and record.expires_at > utc_now()
    )


def _check_rank_limit(limit: int) -> None:
    if not 1 <= limit <= 21:
        raise ValueError("memory rank limit must be between 1 and 21")


def _similarity(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Cosine similarity of two unit vectors, clamped to 0-1."""
    return max(0.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True))))


def _receipt(mutation: MemoryMutation) -> MemoryReceipt:
    return MemoryReceipt(
        key=mutation.record.key, revision=mutation.record.revision, deleted=mutation.record.deleted
    )


class InMemoryLearnedMemoryStore:
    """Ephemeral adapter with the same CAS and replay semantics as PostgreSQL."""

    def __init__(self, quotas: StorageQuotas | None = None) -> None:
        self._records: dict[tuple[str, str, str, str], LearnedMemory] = {}
        self._operations: dict[tuple[str, str, str, str], tuple[str, MemoryReceipt]] = {}
        # One vector per record, for the revision it was computed from.
        self._vectors: dict[tuple[str, str, str, str], tuple[int, MemoryEmbedding]] = {}
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

    async def rank(
        self, scope: MemoryScope, embedding: MemoryEmbedding, limit: int
    ) -> tuple[tuple[LearnedMemory, float], ...]:
        _check_rank_limit(limit)
        ranked: list[tuple[LearnedMemory, float]] = []
        for key, record in self._records.items():
            stored = self._vectors.get(key)
            if (
                key[:3] != _scope(scope)
                or not _active(record)
                or stored is None
                or stored[0] != record.revision
                or stored[1].model != embedding.model
                or len(stored[1].vector) != len(embedding.vector)
            ):
                continue
            ranked.append((record, _similarity(stored[1].vector, embedding.vector)))
        ranked.sort(key=lambda item: (item[1], item[0].updated_at, item[0].key), reverse=True)
        return tuple((record.model_copy(deep=True), score) for record, score in ranked[:limit])

    async def unembedded(
        self, scope: MemoryScope, model: str, limit: int
    ) -> tuple[LearnedMemory, ...]:
        _check_rank_limit(limit)
        rows = [
            record
            for key, record in self._records.items()
            if key[:3] == _scope(scope)
            and _active(record)
            and (
                (stored := self._vectors.get(key)) is None
                or stored[0] != record.revision
                or stored[1].model != model
            )
        ]
        rows.sort(key=lambda r: (r.updated_at, r.key), reverse=True)
        return tuple(r.model_copy(deep=True) for r in rows[:limit])

    async def put_embedding(
        self, scope: MemoryScope, key: str, revision: int, embedding: MemoryEmbedding
    ) -> bool:
        async with self._lock:
            record_key = (*_scope(scope), key)
            record = self._records.get(record_key)
            if record is None or record.revision != revision or not _active(record):
                return False
            self._vectors[record_key] = (revision, embedding)
            return True

    async def apply(self, mutation: MemoryMutation) -> MemoryReceipt:
        return await self.apply_embedded(mutation, None)

    async def apply_embedded(
        self, mutation: MemoryMutation, embedding: MemoryEmbedding | None
    ) -> MemoryReceipt:
        if embedding is not None and mutation.record.deleted:
            raise ValueError("a deleted memory record has no embedding")
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
            if embedding is None:
                self._vectors.pop(key, None)
            else:
                self._vectors[key] = (mutation.record.revision, embedding)
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
                self._vectors.pop(key, None)
            for key, (revision, _) in list(self._vectors.items()):
                record = self._records.get(key)
                if record is None or record.revision != revision or record.content is None:
                    del self._vectors[key]
            return len(keys)


_VECTORS = "algen_agent_runtime_learned_memory_vectors"
# The record's primary key, matched between the learned-memory table (m) and its vectors (v).
_JOIN = "v.tenant_id=m.tenant_id AND v.user_id=m.user_id AND v.agent_id=m.agent_id AND v.key=m.key"
_ACTIVE = "NOT m.deleted AND m.expires_at > now() AND m.record->>'content' IS NOT NULL"
# Inner product of two unit vectors stored as real[] (migration 018). pgvector computes it when
# its extension is installed and visible on the search_path; otherwise plain SQL does.
_PGVECTOR_SCORE = "-(v.embedding::vector <#> $5::real[]::vector)"
_PORTABLE_SCORE = "(SELECT sum(a * b) FROM unnest(v.embedding, $5::real[]) AS t(a, b))"


class PostgresLearnedMemoryStore:
    def __init__(self, database: Any, quotas: StorageQuotas | None = None) -> None:
        self._database = database
        self._quotas = quotas
        self._pgvector: bool | None = None

    async def _score_expression(self) -> str:
        if self._pgvector is None:
            row = await self._database.fetchrow(
                "SELECT EXISTS (SELECT 1 FROM pg_extension e JOIN pg_type t "
                "ON t.typnamespace = e.extnamespace AND t.typname = 'vector' "
                "WHERE e.extname = 'vector' AND t.oid = to_regtype('vector')) AS available"
            )
            self._pgvector = bool(row and row["available"])
        return _PGVECTOR_SCORE if self._pgvector else _PORTABLE_SCORE

    async def rank(
        self, scope: MemoryScope, embedding: MemoryEmbedding, limit: int
    ) -> tuple[tuple[LearnedMemory, float], ...]:
        _check_rank_limit(limit)
        score = await self._score_expression()
        rows = await self._database.fetch(
            f"SELECT m.record, {score} AS score FROM algen_agent_runtime_learned_memory m "
            f"JOIN {_VECTORS} v ON {_JOIN} "
            "WHERE m.tenant_id=$1 AND m.user_id=$2 AND m.agent_id=$3 "
            f"AND {_ACTIVE} AND v.revision=m.revision AND v.model=$4 AND v.dimensions=$6 "
            "ORDER BY score DESC, m.updated_at DESC, m.key DESC LIMIT $7",
            *_scope(scope),
            embedding.model,
            list(embedding.vector),
            len(embedding.vector),
            limit,
        )
        return tuple(
            (self._decode(row["record"]), max(0.0, min(1.0, float(row["score"] or 0.0))))
            for row in rows
        )

    async def unembedded(
        self, scope: MemoryScope, model: str, limit: int
    ) -> tuple[LearnedMemory, ...]:
        _check_rank_limit(limit)
        rows = await self._database.fetch(
            f"SELECT m.record FROM algen_agent_runtime_learned_memory m LEFT JOIN {_VECTORS} v "
            f"ON {_JOIN} WHERE m.tenant_id=$1 AND m.user_id=$2 AND m.agent_id=$3 AND {_ACTIVE} "
            "AND (v.key IS NULL OR v.model<>$4 OR v.revision<>m.revision) "
            "ORDER BY m.updated_at DESC, m.key DESC LIMIT $5",
            *_scope(scope),
            model,
            limit,
        )
        return tuple(self._decode(row["record"]) for row in rows)

    async def put_embedding(
        self, scope: MemoryScope, key: str, revision: int, embedding: MemoryEmbedding
    ) -> bool:
        async with self._database.transaction() as connection:
            # FOR SHARE waits for a concurrent correction or deletion of the record, so a vector
            # of superseded content is never written after the write that removed it.
            row = await connection.fetchrow(
                "SELECT m.revision FROM algen_agent_runtime_learned_memory m "
                f"WHERE m.tenant_id=$1 AND m.user_id=$2 AND m.agent_id=$3 AND m.key=$4 AND {_ACTIVE} "
                "FOR SHARE",
                *_scope(scope),
                key,
            )
            if row is None or row["revision"] != revision:
                return False
            await self._write_vector(connection, scope, key, revision, embedding)
            return True

    @staticmethod
    async def _write_vector(
        connection: Any, scope: MemoryScope, key: str, revision: int, embedding: MemoryEmbedding
    ) -> None:
        await connection.execute(
            f"INSERT INTO {_VECTORS} (tenant_id,user_id,agent_id,key,revision,model,dimensions,embedding) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::real[]) ON CONFLICT (tenant_id,user_id,agent_id,key) "
            "DO UPDATE SET revision=EXCLUDED.revision, model=EXCLUDED.model, "
            "dimensions=EXCLUDED.dimensions, embedding=EXCLUDED.embedding",
            *_scope(scope),
            key,
            revision,
            embedding.model,
            len(embedding.vector),
            list(embedding.vector),
        )

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
        return await self.apply_embedded(mutation, None)

    async def apply_embedded(
        self, mutation: MemoryMutation, embedding: MemoryEmbedding | None
    ) -> MemoryReceipt:
        if embedding is not None and mutation.record.deleted:
            raise ValueError("a deleted memory record has no embedding")
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
                # The vector follows the revision: replaced with a new one, removed otherwise.
                if embedding is None:
                    await connection.execute(
                        f"DELETE FROM {_VECTORS} WHERE tenant_id=$1 AND user_id=$2 AND agent_id=$3 AND key=$4",
                        *scope,
                        record.key,
                    )
                else:
                    await self._write_vector(
                        connection, mutation.scope, record.key, record.revision, embedding
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
            "WHERE expires_at <= now() AND record->>'content' IS NOT NULL LIMIT $1 FOR UPDATE SKIP LOCKED), "
            "purged AS (UPDATE algen_agent_runtime_learned_memory AS m SET record=jsonb_set(jsonb_set(m.record, '{content}', 'null'::jsonb), '{evidence}', 'null'::jsonb) "
            "FROM expired e WHERE m.tenant_id=e.tenant_id AND m.user_id=e.user_id AND m.agent_id=e.agent_id AND m.key=e.key "
            "RETURNING m.tenant_id, m.user_id, m.agent_id, m.key), "
            f"dropped AS (DELETE FROM {_VECTORS} v USING purged m WHERE {_JOIN}) "
            "SELECT key FROM purged",
            limit,
        )
        # Vectors a release without migration 018's handling left behind (after a rollback): of
        # a record it deleted or corrected. They are never ranked, but hold superseded content.
        await self._database.execute(
            f"DELETE FROM {_VECTORS} WHERE ctid IN (SELECT v.ctid FROM {_VECTORS} v "
            f"JOIN algen_agent_runtime_learned_memory m ON {_JOIN} WHERE m.deleted "
            "OR m.record->>'content' IS NULL OR v.revision<>m.revision LIMIT $1)",
            limit,
        )
        return len(rows)
