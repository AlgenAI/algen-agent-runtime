from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.config.settings import MaintenanceSettings
from algen_agent_runtime.orchestration.lifecycle import OwnedWorkflows
from algen_agent_runtime.persistence.retention import RetentionPolicy, RetentionSweeper
from algen_agent_runtime.types.interfaces import ArtifactStore

if TYPE_CHECKING:
    from algen_agent_runtime.runtime.runtime import AgentRuntime


class MaintenanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expired_paused_runs: int = 0
    # Service-owned workflow runs whose approval or input wait was expired in this sweep.
    expired_workflow_pauses: int = 0
    reconciled_pending_turns: int = 0
    purged_artifacts: int = 0
    purged_learned_memory: int = 0
    deleted: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)


class MaintenanceService:
    """Expires stale paused runs and removes expired or out-of-retention records.

    Each task is isolated: a failure is logged and reported without stopping the others,
    and the background loop keeps running so a transient database error is retried on the
    next interval. Sweeps are safe to run from several replicas at once.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        artifacts: ArtifactStore,
        settings: MaintenanceSettings,
        sweeper: RetentionSweeper | None = None,
        conversations: Any | None = None,
    ) -> None:
        self._runtime = runtime
        # Ends conversation turns whose paused run the sweep just expired.
        self._conversations = conversations
        self._artifacts = artifacts
        self._settings = settings
        self._sweeper = sweeper
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._logger = structlog.get_logger("algen_agent_runtime.maintenance")
        self._workflows: OwnedWorkflows | None = None

    def attach_workflows(self, workflows: OwnedWorkflows) -> None:
        """Expire the waits of the workflow runs this service owns on every sweep."""
        self._workflows = workflows

    @property
    def policy(self) -> RetentionPolicy:
        settings = self._settings
        return RetentionPolicy(
            terminal_runs=_days(settings.terminal_run_retention_days),
            audit_events=_days(settings.audit_retention_days),
            conversations=_days(settings.conversation_retention_days),
            workflow_checkpoints=_days(settings.workflow_checkpoint_retention_days),
            analytical_graphs=_days(settings.analytical_graph_retention_days),
            work_items=_days(settings.work_item_retention_days),
            batch_size=settings.batch_size,
            max_batches=settings.max_batches_per_sweep,
        )

    async def run_once(self) -> MaintenanceReport:
        report = MaintenanceReport()
        settings = self._settings
        if settings.expire_paused_runs:

            async def expire() -> None:
                report.expired_paused_runs = await self._runtime.expire_paused_runs(
                    clarification_timeout_seconds=settings.clarification_timeout_seconds,
                    limit=settings.batch_size * settings.max_batches_per_sweep,
                )

            await self._guard("paused_runs", expire, report)
            conversations = self._conversations
            if conversations is not None:

                async def reconcile_turns() -> None:
                    report.reconciled_pending_turns = await conversations.reconcile_pending_turns(
                        settings.batch_size * settings.max_batches_per_sweep
                    )

                await self._guard("pending_turns", reconcile_turns, report)
        workflows = self._workflows
        if settings.expire_paused_workflows and workflows is not None:

            async def expire_workflows() -> None:
                report.expired_workflow_pauses = await workflows.expire_paused(
                    limit=settings.batch_size * settings.max_batches_per_sweep,
                    input_timeout_seconds=settings.clarification_timeout_seconds,
                )

            await self._guard("paused_workflows", expire_workflows, report)

        async def purge_artifacts() -> None:
            for _ in range(settings.max_batches_per_sweep):
                count = await self._artifacts.purge_expired(limit=settings.batch_size)
                report.purged_artifacts += count
                if count < settings.batch_size:
                    return

        await self._guard("artifacts", purge_artifacts, report)
        learned_memory = self._runtime.learned_memory
        if learned_memory is not None:

            async def purge_memory() -> None:
                for _ in range(settings.max_batches_per_sweep):
                    count = await learned_memory.store.purge_expired(settings.batch_size)
                    report.purged_learned_memory += count
                    if count < settings.batch_size:
                        return

            await self._guard("learned_memory", purge_memory, report)
        sweeper = self._sweeper
        if sweeper is not None:

            async def sweep() -> None:
                report.deleted = (await sweeper.sweep(self.policy)).deleted

            await self._guard("retention", sweep, report)
        self._logger.info("maintenance_completed", **report.model_dump())
        return report

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stopping.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self._settings.interval_seconds)
            if self._stopping.is_set():
                return
            await self.run_once()

    async def _guard(
        self, name: str, task: Callable[[], Awaitable[None]], report: MaintenanceReport
    ) -> None:
        try:
            await task()
        except Exception as exc:
            report.errors[name] = type(exc).__name__
            self._logger.exception("maintenance_task_failed", task=name)


def _days(value: int | None) -> timedelta | None:
    return timedelta(days=value) if value is not None else None
