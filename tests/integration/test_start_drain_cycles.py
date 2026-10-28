"""Repeated start, run, drain and restart cycles of the service.

Each cycle builds a new container and app on the same stores (one set of in-memory stores shared
across cycles, or a live PostgreSQL database), as a restarted process would. A cycle checks that
the work the previous drain interrupted is recovered exactly once, that new work runs, that
readiness turns false while the service drains, and that nothing is left behind: no task, no
open pool and, on PostgreSQL, no extra server connection.
"""

from __future__ import annotations

import asyncio
import os
from collections import Counter
from typing import Any
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers import mock as mock_module
from algen_agent_runtime.orchestration import container as container_module
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.persistence.postgres import PostgresDatabase
from algen_agent_runtime.types.contracts import TERMINAL_STATUSES, RunRequest, RunStatus
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor
from algen_agent_runtime.workflows.contracts import WorkflowStatus
from algen_agent_runtime.workflows.service import WorkflowService

CYCLES = 3
WORKFLOW = {
    "name": "nightly",
    "version": "1.0.0",
    "output_key": "report",
    "nodes": [
        {
            "id": "work",
            "kind": "agent",
            "agent": "worker",
            "output_key": "report",
            "input_template": "{{input}}",
            "recovery_policy": "retry",
        }
    ],
}


class GatedProvider(mock_module.MockModelProvider):
    """While ``blocking``, every generation waits until it is cancelled (by a drain)."""

    blocking = False
    entered = 0

    async def generate(self, request: Any) -> Any:
        if type(self).blocking:
            type(self).entered += 1
            await asyncio.Event().wait()
        return await super().generate(request)


def _settings(storage: dict[str, Any] | None = None) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "worker",
                    "version": "1.0.0",
                    "description": "Works",
                    "system_instructions": "Do the work.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                }
            ],
            "workflows": {"nightly": WORKFLOW},
            "runtime": {"shutdown_grace_seconds": 0.05},
            **({"storage": storage} if storage else {}),
        }
    )


async def _until(condition: Any, what: str) -> None:
    for _ in range(1000):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


async def _cycles(monkeypatch: pytest.MonkeyPatch, settings: AppSettings, tenant: str) -> None:
    monkeypatch.setattr(mock_module, "MockModelProvider", GatedProvider)
    recoveries: Counter[str] = Counter()
    recover = MultiAgentWorkflowExecutor.recover

    async def counted(self: Any, manifest: Any, workflow_id: str, **kwargs: Any) -> Any:
        recoveries[workflow_id] += 1
        return await recover(self, manifest, workflow_id, **kwargs)

    monkeypatch.setattr(MultiAgentWorkflowExecutor, "recover", counted)
    interrupted_workflows: list[str] = []
    interrupted_runs: list[str] = []
    started: list[str] = []
    for cycle in range(CYCLES):
        GatedProvider.blocking, GatedProvider.entered = False, 0
        before = asyncio.all_tasks()
        container = build_container(settings)
        app = create_app(settings, container)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        service = container.lifecycle.workflows
        assert isinstance(service, WorkflowService)
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
        try:
            # What the previous drain interrupted is carried on, once.
            await service.idle()
            for workflow_id in interrupted_workflows:
                state = await container.workflow_checkpoints.get(workflow_id, tenant)
                assert state is not None and state.status is WorkflowStatus.COMPLETED, cycle
                assert recoveries[workflow_id] == 1
            for run_id in interrupted_runs:
                result = await container.runtime.wait(run_id, tenant)
                assert result.status is RunStatus.COMPLETED, cycle
            ready = await client.get("/health/ready")
            assert ready.status_code == 200 and ready.json()["status"] == "ready"

            # New work runs.
            done = await container.runtime.run(
                RunRequest(agent="worker", input="quick", tenant_id=tenant, user_id="u")
            )
            assert done.status is RunStatus.COMPLETED

            # Work still in flight when the service drains.
            GatedProvider.blocking = True
            workflow_id = f"nightly-{uuid4().hex[:12]}"
            started.append(workflow_id)
            await service.start("nightly", tenant_id=tenant, user_id="u", workflow_id=workflow_id)
            run = await container.runtime.start(
                RunRequest(agent="worker", input="slow", tenant_id=tenant, user_id="u")
            )
            await _until(lambda: GatedProvider.entered >= 2, "in-flight work")

            closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            lifecycle = container.lifecycle
            await _until(lambda: lifecycle.phase != "ready", "drain")  # noqa: B023
            draining = await client.get("/health/ready")
            assert draining.status_code == 503 and draining.json()["status"] == "draining"
            await closing
        finally:
            await client.aclose()
        assert container.lifecycle.phase == "stopped"

        # Nothing is left running or open.
        await asyncio.sleep(0)
        leaked = [task for task in asyncio.all_tasks() - before if not task.done()]
        assert leaked == [], leaked
        assert not service._tasks and not service._active
        databases = [item for item in container.resources if isinstance(item, PostgresDatabase)]
        assert all(database._pool is None for database in databases)
        assert all(
            item.is_closed for item in container.resources if isinstance(item, httpx.AsyncClient)
        )

        # Interrupted, not cancelled: the next start recovers both. (Reading the stores reopens
        # the pool, so it is closed again afterwards.)
        state = await container.workflow_checkpoints.get(workflow_id, tenant)
        assert state is not None and state.status is WorkflowStatus.RUNNING
        status = (await container.runtime.status(run.id, tenant)).status
        assert status not in TERMINAL_STATUSES
        interrupted_workflows, interrupted_runs = [workflow_id], [run.id]
        for database in databases:
            await database.close()
    # Each interrupted run was recovered by exactly one later start (a shared database may hold
    # other tests' runs, so only this test's are counted).
    assert [recoveries[workflow_id] for workflow_id in started] == [1] * (CYCLES - 1) + [0]


async def test_repeated_start_drain_cycles_on_in_memory_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One set of stores outlives each container, as a database outlives a process.
    for name in (
        "InMemoryRunStore",
        "InMemoryEventBus",
        "InMemoryAuditLog",
        "InMemoryApprovalService",
        "InMemoryToolExecutionStore",
        "InMemoryWorkflowCheckpointStore",
        "InMemoryConversationStore",
        "InMemoryConversationEventBus",
    ):
        shared = getattr(container_module, name)()
        monkeypatch.setattr(container_module, name, lambda shared=shared: shared)
    await _cycles(monkeypatch, _settings(), "acme")


async def _connections(dsn: str) -> int:
    import asyncpg

    connection = await asyncpg.connect(dsn)
    try:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
    finally:
        await connection.close()


@pytest.mark.integration
@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("POSTGRES_DSN"), reason="requires a live PostgreSQL")
async def test_repeated_start_drain_cycles_on_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("asyncpg")
    dsn = os.environ["POSTGRES_DSN"]
    monkeypatch.setenv("CYCLE_POSTGRES_DSN", dsn)
    settings = _settings(
        {
            "run_store": "postgres",
            "event_store": "postgres",
            "audit_store": "postgres",
            "approval_store": "postgres",
            "artifact_store": "postgres",
            "workflow_store": "postgres",
            "tool_execution_store": "postgres",
            "conversation_store": "postgres",
            "postgres_dsn": "env://CYCLE_POSTGRES_DSN",
        }
    )
    connections = await _connections(dsn)

    await _cycles(monkeypatch, settings, f"cycles-{uuid4().hex[:8]}")

    for _ in range(100):
        if await _connections(dsn) <= connections:
            break
        await asyncio.sleep(0.02)
    assert await _connections(dsn) <= connections
