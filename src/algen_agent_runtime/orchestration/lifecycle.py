"""Where a container is in its life, and the service-owned work that follows its start and drain.

``Container.astart`` moves the phase ``created`` -> ``starting`` -> ``ready``; ``Container.aclose``
moves it to ``draining`` and then ``stopped``. ``/health/ready`` is false while draining or
stopped, so a load balancer stops routing before work is interrupted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Phase = Literal["created", "starting", "ready", "draining", "stopped"]


class OwnedWorkflows(Protocol):
    """Workflow runs the service owns (``workflows.service.WorkflowService``)."""

    async def recover(self, limit: int = 1000) -> int: ...

    async def expire_paused(
        self, *, limit: int = 1000, input_timeout_seconds: float | None = None
    ) -> int: ...

    async def aclose(self, grace_seconds: float = 0) -> None: ...


@dataclass
class ContainerLifecycle:
    phase: Phase = "created"
    workflows: OwnedWorkflows | None = None
