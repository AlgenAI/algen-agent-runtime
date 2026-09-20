from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from algen_agent_runtime.distributed.contracts import WorkItem, WorkStatus
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import utc_now


class InMemoryWorkQueue:
    def __init__(self) -> None:
        self._items: dict[str, WorkItem] = {}
        self._deduplication: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, item: WorkItem) -> tuple[WorkItem, bool]:
        async with self._lock:
            key = (item.tenant_id, item.deduplication_key)
            current_id = self._deduplication.get(key)
            if current_id:
                return self._items[current_id].model_copy(deep=True), False
            self._items[item.id] = item.model_copy(deep=True)
            self._deduplication[key] = item.id
            return item.model_copy(deep=True), True

    async def claim(self, worker_id: str, lease_seconds: int) -> WorkItem | None:
        now = utc_now()
        async with self._lock:
            eligible = [
                item
                for item in self._items.values()
                if item.available_at <= now
                and (
                    item.status == WorkStatus.PENDING
                    or (
                        item.status == WorkStatus.LEASED
                        and item.lease_expires_at is not None
                        and item.lease_expires_at <= now
                    )
                )
                and item.attempts < item.maximum_attempts
            ]
            if not eligible:
                return None
            item = sorted(eligible, key=lambda value: (value.available_at, value.created_at))[0]
            item.status = WorkStatus.LEASED
            item.lease_owner = worker_id
            item.lease_expires_at = now + timedelta(seconds=lease_seconds)
            item.attempts += 1
            item.updated_at = now
            return item.model_copy(deep=True)

    async def renew(self, item_id: str, worker_id: str, lease_seconds: int) -> bool:
        async with self._lock:
            item = self._owned(item_id, worker_id)
            item.lease_expires_at = utc_now() + timedelta(seconds=lease_seconds)
            item.updated_at = utc_now()
            return True

    async def complete(self, item_id: str, worker_id: str, result: dict[str, object]) -> None:
        async with self._lock:
            item = self._owned(item_id, worker_id)
            item.status = WorkStatus.COMPLETED
            item.result = dict(result)
            item.lease_owner = None
            item.lease_expires_at = None
            item.updated_at = utc_now()

    async def fail(self, item_id: str, worker_id: str, error: str, retry: bool) -> None:
        async with self._lock:
            item = self._owned(item_id, worker_id)
            item.error = error
            item.status = (
                WorkStatus.PENDING
                if retry and item.attempts < item.maximum_attempts
                else WorkStatus.FAILED
            )
            item.lease_owner = None
            item.lease_expires_at = None
            item.updated_at = utc_now()

    def _owned(self, item_id: str, worker_id: str) -> WorkItem:
        item = self._items.get(item_id)
        if item is None or item.status != WorkStatus.LEASED or item.lease_owner != worker_id:
            raise ConflictError("work item lease is not owned by this worker")
        return item


class PostgresWorkQueue:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def enqueue(self, item: WorkItem) -> tuple[WorkItem, bool]:
        result = await self._database.execute(
            "INSERT INTO algen_agent_runtime_work_items "
            "(id,tenant_id,kind,deduplication_key,status,attempts,maximum_attempts,available_at,item) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb) "
            "ON CONFLICT (tenant_id,deduplication_key) DO NOTHING",
            item.id,
            item.tenant_id,
            item.kind,
            item.deduplication_key,
            item.status.value,
            item.attempts,
            item.maximum_attempts,
            item.available_at,
            item.model_dump_json(),
        )
        if result == "INSERT 0 1":
            return item, True
        row = await self._database.fetchrow(
            "SELECT item FROM algen_agent_runtime_work_items WHERE tenant_id=$1 AND deduplication_key=$2",
            item.tenant_id,
            item.deduplication_key,
        )
        return WorkItem.model_validate_json(row["item"]), False

    async def claim(self, worker_id: str, lease_seconds: int) -> WorkItem | None:
        row = await self._database.fetchrow(
            "WITH candidate AS (SELECT id FROM algen_agent_runtime_work_items "
            "WHERE available_at<=now() AND attempts<maximum_attempts AND "
            "(status='pending' OR (status='leased' AND lease_expires_at<=now())) "
            "ORDER BY available_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE algen_agent_runtime_work_items w SET status='leased',lease_owner=$1,"
            "lease_expires_at=now()+($2*interval '1 second'),attempts=w.attempts+1,updated_at=now(),"
            "item=jsonb_set(jsonb_set(jsonb_set(jsonb_set(w.item,'{status}','\"leased\"'),"
            "'{lease_owner}',to_jsonb($1::text)),'{lease_expires_at}',to_jsonb(now()+($2*interval '1 second'))),"
            "'{attempts}',to_jsonb(w.attempts+1)) FROM candidate WHERE w.id=candidate.id RETURNING w.item",
            worker_id,
            lease_seconds,
        )
        return WorkItem.model_validate_json(row["item"]) if row else None

    async def renew(self, item_id: str, worker_id: str, lease_seconds: int) -> bool:
        result = await self._database.execute(
            "UPDATE algen_agent_runtime_work_items SET lease_expires_at=now()+($1*interval '1 second'),"
            "item=jsonb_set(item,'{lease_expires_at}',to_jsonb(now()+($1*interval '1 second'))),"
            "updated_at=now() WHERE id=$2 AND lease_owner=$3 AND status='leased'",
            lease_seconds,
            item_id,
            worker_id,
        )
        return bool(result == "UPDATE 1")

    async def complete(self, item_id: str, worker_id: str, result: dict[str, object]) -> None:
        updated = await self._database.execute(
            "UPDATE algen_agent_runtime_work_items SET status='completed',result=$1::jsonb,"
            "item=jsonb_set(jsonb_set(jsonb_set(jsonb_set("
            "jsonb_set(item,'{status}','\"completed\"'),'{result}',$1::jsonb),"
            "'{lease_owner}','null'::jsonb),'{lease_expires_at}','null'::jsonb),"
            "'{updated_at}',to_jsonb(now())),"
            "lease_owner=NULL,lease_expires_at=NULL,updated_at=now() "
            "WHERE id=$2 AND lease_owner=$3 AND status='leased'",
            __import__("json").dumps(result),
            item_id,
            worker_id,
        )
        if updated != "UPDATE 1":
            raise ConflictError("work item lease is not owned by this worker")

    async def fail(self, item_id: str, worker_id: str, error: str, retry: bool) -> None:
        status = "pending" if retry else "failed"
        updated = await self._database.execute(
            "UPDATE algen_agent_runtime_work_items SET "
            "status=CASE WHEN $1='pending' AND attempts<maximum_attempts THEN 'pending' ELSE 'failed' END,"
            "error=$2,"
            "item=jsonb_set(jsonb_set(jsonb_set(jsonb_set(jsonb_set("
            "item,'{status}',to_jsonb((CASE WHEN $1='pending' "
            "AND attempts<maximum_attempts THEN 'pending' ELSE 'failed' END)::text)),"
            "'{error}',to_jsonb($2::text)),'{lease_owner}','null'::jsonb),"
            "'{lease_expires_at}','null'::jsonb),'{updated_at}',to_jsonb(now())),"
            "lease_owner=NULL,lease_expires_at=NULL,updated_at=now() "
            "WHERE id=$3 AND lease_owner=$4 AND status='leased'",
            status,
            error,
            item_id,
            worker_id,
        )
        if updated != "UPDATE 1":
            raise ConflictError("work item lease is not owned by this worker")
