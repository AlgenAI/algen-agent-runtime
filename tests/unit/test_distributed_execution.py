from algen_agent_runtime.distributed import InMemoryWorkQueue, WorkItem, WorkStatus


async def test_work_queue_deduplicates_and_enforces_lease_ownership() -> None:
    queue = InMemoryWorkQueue()
    item = WorkItem(
        tenant_id="t1",
        kind="analytical_graph",
        deduplication_key="graph-1",
        payload={"graph_id": "g1"},
    )
    _, created = await queue.enqueue(item)
    duplicate, duplicate_created = await queue.enqueue(item.model_copy(update={"id": "other"}))
    assert created and not duplicate_created and duplicate.id == item.id
    claimed = await queue.claim("worker-a", 30)
    assert claimed is not None and claimed.status == WorkStatus.LEASED
    await queue.complete(claimed.id, "worker-a", {"status": "done"})
    assert await queue.claim("worker-b", 30) is None
