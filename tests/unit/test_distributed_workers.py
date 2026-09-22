from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from algen_agent_runtime.distributed.contracts import WorkItem, WorkStatus
from algen_agent_runtime.distributed.queue import InMemoryWorkQueue
from algen_agent_runtime.distributed.worker import DistributedWorker
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import utc_now


def _make_item(
    item_id: str = "item-1",
    tenant_id: str = "tenant-a",
    kind: str = "test-task",
    deduplication_key: str = "dedup-1",
    payload: dict[str, Any] | None = None,
    maximum_attempts: int = 3,
) -> WorkItem:
    return WorkItem(
        id=item_id,
        tenant_id=tenant_id,
        kind=kind,
        deduplication_key=deduplication_key,
        payload=payload or {"data": "test"},
        maximum_attempts=maximum_attempts,
    )


@pytest.mark.asyncio
async def test_in_memory_work_queue_deduplication() -> None:
    queue = InMemoryWorkQueue()
    item1 = _make_item("item-1", tenant_id="tenant-a", deduplication_key="key-1")
    item2 = _make_item("item-2", tenant_id="tenant-a", deduplication_key="key-1")
    item3 = _make_item("item-3", tenant_id="tenant-b", deduplication_key="key-1")

    # Enqueue item1
    enqueued1, created1 = await queue.enqueue(item1)
    assert created1 is True
    assert enqueued1.id == "item-1"

    # Enqueue item2 with duplicate key in same tenant returns existing
    enqueued2, created2 = await queue.enqueue(item2)
    assert created2 is False
    assert enqueued2.id == "item-1"

    # Enqueue item3 with same key but different tenant succeeds (tenant isolation)
    enqueued3, created3 = await queue.enqueue(item3)
    assert created3 is True
    assert enqueued3.id == "item-3"


@pytest.mark.asyncio
async def test_in_memory_work_queue_claim_and_renew() -> None:
    queue = InMemoryWorkQueue()
    item = _make_item("item-1")
    await queue.enqueue(item)

    # Claim item
    claimed = await queue.claim("worker-1", lease_seconds=30)
    assert claimed is not None
    assert claimed.id == "item-1"
    assert claimed.status == WorkStatus.LEASED
    assert claimed.lease_owner == "worker-1"
    assert claimed.attempts == 1
    assert claimed.lease_expires_at is not None

    original_expiry = claimed.lease_expires_at

    # Renew lease
    renewed = await queue.renew("item-1", "worker-1", lease_seconds=60)
    assert renewed is True

    # Re-fetch from queue internal state to check extended expiry
    reclaimed = queue._items["item-1"]
    assert reclaimed.lease_expires_at is not None
    assert reclaimed.lease_expires_at > original_expiry


@pytest.mark.asyncio
async def test_worker_fencing_prevents_stale_lease_actions() -> None:
    queue = InMemoryWorkQueue()
    item = _make_item("item-1")
    await queue.enqueue(item)

    # Worker A claims the item
    claimed_a = await queue.claim("worker-a", lease_seconds=1)
    assert claimed_a is not None

    # Simulate lease expiration
    now = utc_now()
    queue._items["item-1"].lease_expires_at = now - timedelta(seconds=10)
    queue._items["item-1"].available_at = now - timedelta(seconds=10)

    # Worker B claims the expired item (steals lease)
    claimed_b = await queue.claim("worker-b", lease_seconds=30)
    assert claimed_b is not None
    assert claimed_b.lease_owner == "worker-b"
    assert claimed_b.attempts == 2

    # Worker A attempts to renew -> fenced out!
    with pytest.raises(ConflictError, match="lease is not owned by this worker"):
        await queue.renew("item-1", "worker-a", lease_seconds=30)

    # Worker A attempts to complete -> fenced out!
    with pytest.raises(ConflictError, match="lease is not owned by this worker"):
        await queue.complete("item-1", "worker-a", {"result": "stale"})

    # Worker B completes successfully
    await queue.complete("item-1", "worker-b", {"result": "fresh"})
    assert queue._items["item-1"].status == WorkStatus.COMPLETED
    assert queue._items["item-1"].result == {"result": "fresh"}


@pytest.mark.asyncio
async def test_distributed_worker_execution_lifecycle() -> None:
    queue = InMemoryWorkQueue()
    worker = DistributedWorker("worker-1", queue, lease_seconds=30)

    # Duplicate handler registration raises ValueError
    worker.register("test-task", lambda item: asyncio.sleep(0, result={"echo": item.payload}))
    with pytest.raises(ValueError, match="already exists"):
        worker.register("test-task", lambda item: asyncio.sleep(0, result={}))

    # Empty queue returns False
    assert await worker.run_once() is False

    # Enqueue and run
    item = _make_item("item-1", payload={"msg": "run me"})
    await queue.enqueue(item)

    ran = await worker.run_once()
    assert ran is True

    # State is completed in queue
    stored = queue._items["item-1"]
    assert stored.status == WorkStatus.COMPLETED
    assert stored.result == {"echo": {"msg": "run me"}}
    assert stored.lease_owner is None
    assert stored.lease_expires_at is None


@pytest.mark.asyncio
async def test_distributed_worker_retry_policy_on_failure() -> None:
    queue = InMemoryWorkQueue()
    worker = DistributedWorker("worker-1", queue, lease_seconds=10)

    attempts = 0

    async def failing_handler(item: WorkItem) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError(f"failure #{attempts}")

    worker.register("failing-task", failing_handler)

    # Maximum 2 attempts
    item = _make_item("item-fail", kind="failing-task", maximum_attempts=2)
    await queue.enqueue(item)

    # Attempt 1: fails, but attempts (1) < maximum_attempts (2) -> resets to PENDING
    assert await worker.run_once() is True
    assert attempts == 1
    assert queue._items["item-fail"].status == WorkStatus.PENDING
    assert queue._items["item-fail"].attempts == 1
    assert queue._items["item-fail"].error == "failure #1"

    # Attempt 2: fails, attempts (2) >= maximum_attempts (2) -> transitions to FAILED
    assert await worker.run_once() is True
    assert attempts == 2
    assert queue._items["item-fail"].status == WorkStatus.FAILED
    assert queue._items["item-fail"].attempts == 2
    assert queue._items["item-fail"].error == "failure #2"

    # Subsequent run finds no eligible work
    assert await worker.run_once() is False


@pytest.mark.asyncio
async def test_distributed_worker_handles_cancellation() -> None:
    queue = InMemoryWorkQueue()
    worker = DistributedWorker("worker-1", queue, lease_seconds=10)

    async def cancelled_handler(item: WorkItem) -> dict[str, Any]:
        raise asyncio.CancelledError()

    worker.register("cancel-task", cancelled_handler)

    item = _make_item("item-cancel", kind="cancel-task")
    await queue.enqueue(item)

    with pytest.raises(asyncio.CancelledError):
        await worker.run_once()

    # Worker caught CancelledError, reported failure with retry, and re-raised
    stored = queue._items["item-cancel"]
    assert stored.status == WorkStatus.PENDING
    assert stored.error == "worker cancelled"
