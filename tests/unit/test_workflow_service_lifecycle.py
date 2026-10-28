"""The workflow service's runs across the service lifecycle: recovery, expiry and drain."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.models.providers import mock as mock_module
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.types.contracts import utc_now
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowExecutionState,
    WorkflowNodeExecution,
    WorkflowNodeStatus,
    WorkflowStatus,
)
from algen_agent_runtime.workflows.service import OWNER, WorkflowService

PAST = "2000-01-01T00:00:00+00:00"
AGENT = {
    "name": "worker",
    "version": "1.0.0",
    "description": "Works",
    "system_instructions": "Do the work.",
    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
}
WORKFLOWS = {
    "screening": {
        "name": "screening",
        "version": "1.0.0",
        "nodes": [
            {
                "id": "review",
                "kind": "approval",
                "output_key": "approval",
                "approval": {"prompt": "Screen this application?"},
            },
            {
                "id": "screen",
                "kind": "agent",
                "agent": "worker",
                "depends_on": ["review"],
                "output_key": "screened",
                "input_template": "{{input}}",
            },
        ],
    },
    "nightly": {
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
    },
    "hooked": {
        "name": "hooked",
        "version": "1.0.0",
        "hook_provider": "somewhere.hooks:create_workflow_hooks",
        "nodes": [{"id": "go", "kind": "handler", "handler": "go", "output_key": "out"}],
    },
}


def _settings(**extra: Any) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [AGENT],
            "workflows": WORKFLOWS,
            **extra,
        }
    )


def _service(container: Container) -> WorkflowService:
    service = container.lifecycle.workflows
    assert isinstance(service, WorkflowService)
    return service


async def _age_approval(container: Container, workflow_id: str, tenant_id: str) -> None:
    store = container.workflow_checkpoints
    state = await store.get(workflow_id, tenant_id)
    assert state is not None and state.pause is not None
    state.pause["expires_at"] = PAST
    await store.save(state, state.version)


async def _status(container: Container, workflow_id: str, tenant_id: str) -> WorkflowStatus:
    state = await container.workflow_checkpoints.get(workflow_id, tenant_id)
    assert state is not None
    return state.status


def _interrupted(name: str, workflow_id: str, *, owner: str | None = OWNER) -> Any:
    """A checkpoint as a process that stopped mid-run leaves it."""
    return WorkflowExecutionState(
        id=workflow_id,
        manifest_name=name,
        manifest_version="1.0.0",
        tenant_id="acme",
        user_id="scheduler",
        owner=owner,
        status=WorkflowStatus.RUNNING,
        values={"input": "nightly report"},
        nodes={
            "work": WorkflowNodeExecution(
                node_id="work", status=WorkflowNodeStatus.RUNNING, started_at=utc_now()
            )
        }
        if name == "nightly"
        else {"go": WorkflowNodeExecution(node_id="go", status=WorkflowNodeStatus.RUNNING)},
    )


async def test_expired_approvals_of_service_runs_are_expired_per_tenant_and_once() -> None:
    container = build_container(_settings())
    create_app(_settings(), container)
    service = _service(container)
    for tenant in ("acme", "globex"):
        await service.start("screening", tenant_id=tenant, user_id="hr", workflow_id="app-0001")
    await _age_approval(container, "app-0001", "acme")
    # A run an application drives with its own executor is not the service's to expire.
    own = MultiAgentWorkflowExecutor(
        object(), WorkflowHookRegistry(), store=container.workflow_checkpoints
    )
    manifest = container.lifecycle.workflows._settings.workflows["screening"]  # type: ignore[union-attr]
    app_run = await own.run(manifest, {"input": "x"}, tenant_id="acme", user_id="app")
    await _age_approval(container, app_run.id, "acme")

    assert await service.expire_paused() == 1
    await service.idle()
    assert await _status(container, "app-0001", "acme") is WorkflowStatus.FAILED
    assert await _status(container, "app-0001", "globex") is WorkflowStatus.AWAITING_APPROVAL
    assert await _status(container, app_run.id, "acme") is WorkflowStatus.AWAITING_APPROVAL
    state = await container.workflow_checkpoints.get("app-0001", "acme")
    assert state is not None and state.owner == OWNER
    assert state.nodes["review"].error == "workflow approval expired"
    assert state.nodes["screen"].status is WorkflowNodeStatus.SKIPPED
    # A second sweep finds nothing left to expire.
    assert await service.expire_paused() == 0
    await container.aclose()


async def test_the_maintenance_sweep_expires_service_workflow_waits() -> None:
    for enabled, expected in ((True, 1), (False, 0)):
        settings = _settings(maintenance={"expire_paused_workflows": enabled})
        container = build_container(settings)
        create_app(settings, container)
        service = _service(container)
        await service.start("screening", tenant_id="acme", user_id="hr", workflow_id="app-0002")
        await _age_approval(container, "app-0002", "acme")
        assert container.maintenance is not None

        report = await container.maintenance.run_once()
        await service.idle()

        assert report.expired_workflow_pauses == expected
        assert report.errors == {}
        assert (await _status(container, "app-0002", "acme") is WorkflowStatus.FAILED) is enabled
        await container.aclose()


async def test_input_waits_expire_through_the_sweep_only_with_a_timeout() -> None:
    settings = _settings(maintenance={"clarification_timeout_seconds": 60})
    container = build_container(settings)
    create_app(settings, container)
    state = _interrupted("nightly", "nightly-0001").model_copy(
        update={
            "status": WorkflowStatus.AWAITING_INPUT,
            "pause": {"node_id": "work", "question": "Which region?", "requested_at": PAST},
        }
    )
    state.nodes["work"].status = WorkflowNodeStatus.COMPLETED
    await container.workflow_checkpoints.create(state)
    assert container.maintenance is not None

    report = await container.maintenance.run_once()
    await _service(container).idle()

    assert report.expired_workflow_pauses == 1
    expired = await container.workflow_checkpoints.get("nightly-0001", "acme")
    assert expired is not None and expired.nodes["work"].error == "workflow input expired"
    await container.aclose()


async def test_start_recovers_only_the_service_runs_it_can_drive_exactly() -> None:
    settings = _settings()
    container = build_container(settings)
    store = container.workflow_checkpoints
    await store.create(_interrupted("nightly", "nightly-0002"))
    await store.create(_interrupted("nightly", "nightly-0003", owner=None))
    # An untrusted hook provider and a version no longer configured are left for an operator.
    await store.create(_interrupted("hooked", "hooked-0001"))
    await store.create(
        _interrupted("nightly", "nightly-0004").model_copy(update={"manifest_version": "0.9.0"})
    )
    app = create_app(settings, container)

    async with app.router.lifespan_context(app):
        await _service(container).idle()
        recovered = await store.get("nightly-0002", "acme")
        assert recovered is not None and recovered.status is WorkflowStatus.COMPLETED
        assert recovered.values["report"]
        for left in ("nightly-0003", "hooked-0001", "nightly-0004"):
            assert await _status(container, left, "acme") is WorkflowStatus.RUNNING


@pytest.mark.parametrize(
    "runtime",
    [{"recover_service_workflows": False}, {"recover_incomplete_runs": False}],
)
async def test_workflow_recovery_can_be_turned_off(runtime: dict[str, bool]) -> None:
    settings = _settings(runtime=runtime)
    container = build_container(settings)
    await container.workflow_checkpoints.create(_interrupted("nightly", "nightly-0005"))
    app = create_app(settings, container)

    async with app.router.lifespan_context(app):
        await _service(container).idle()
        assert await _status(container, "nightly-0005", "acme") is WorkflowStatus.RUNNING


async def test_a_failing_recovery_is_reported_as_degraded_instead_of_stopping_start() -> None:
    settings = _settings()
    container = build_container(settings)
    app = create_app(settings, container)

    async def broken(limit: int = 1000) -> int:
        raise RuntimeError("hook import failed for postgresql://u:p@h/db")

    _service(container).recover = broken  # type: ignore[method-assign]
    async with app.router.lifespan_context(app):
        ready, body = await container.readiness_report()
        assert ready and body["status"] == "degraded"
        assert body["degraded_dependencies"] == {"workflow.recovery": "RuntimeError"}


class GatedProvider(mock_module.MockModelProvider):
    """Blocks every generation until ``release`` is set."""

    release = asyncio.Event()
    entered = 0

    async def generate(self, request: Any) -> Any:
        type(self).entered += 1
        await type(self).release.wait()
        return await super().generate(request)


async def test_drain_leaves_an_in_flight_run_recoverable_and_refuses_new_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    GatedProvider.release = asyncio.Event()
    GatedProvider.entered = 0
    monkeypatch.setattr(mock_module, "MockModelProvider", GatedProvider)
    settings = _settings(runtime={"shutdown_grace_seconds": 0.05})
    container = build_container(settings)
    app = create_app(settings, container)
    service = _service(container)

    async with app.router.lifespan_context(app):
        await service.start("nightly", tenant_id="acme", user_id="s", workflow_id="nightly-0006")
        for _ in range(500):
            if GatedProvider.entered:
                break
            await asyncio.sleep(0.002)
        assert GatedProvider.entered == 1

    state = await container.workflow_checkpoints.get("nightly-0006", "acme")
    assert state is not None
    assert state.status is WorkflowStatus.RUNNING
    assert state.nodes["work"].status is WorkflowNodeStatus.RUNNING
    assert not service._tasks and not service._active
    with pytest.raises(ConflictError):
        await service.start("nightly", tenant_id="acme", user_id="s")
