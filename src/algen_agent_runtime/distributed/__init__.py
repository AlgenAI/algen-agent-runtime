"""Experimental: work queues and workers for horizontal scaling.

Outside the compatibility promise (``docs/api-stability.md``, "Experimental interfaces"): the
``1.0`` deployment scope is a single node, and multi-worker recovery, fencing and distributed
limits are not complete. Every name here can change in a minor release, with an upgrade note.
"""

from algen_agent_runtime.distributed.contracts import WorkItem, WorkQueue, WorkStatus
from algen_agent_runtime.distributed.queue import InMemoryWorkQueue, PostgresWorkQueue
from algen_agent_runtime.distributed.worker import DistributedWorker

#: Marks this package experimental (``algen_agent_runtime.stability``).
__experimental__ = (
    "horizontal scaling is outside the 1.0 deployment scope (a single node); multi-worker "
    "recovery, fencing and distributed limits are not complete"
)

__all__ = [
    "DistributedWorker",
    "InMemoryWorkQueue",
    "PostgresWorkQueue",
    "WorkItem",
    "WorkQueue",
    "WorkStatus",
]
