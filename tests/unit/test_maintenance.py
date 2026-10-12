from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.cli import build_parser
from algen_agent_runtime.config.settings import AppSettings, MaintenanceSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.orchestration.maintenance import MaintenanceService
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.persistence.retention import (
    PostgresRetentionSweeper,
    RetentionPolicy,
    RetentionReport,
)
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import (
    Artifact,
    RunRequest,
    RunState,
    RunStatus,
    utc_now,
)


def _approval_runtime() -> AgentRuntime:
    runtime = make_runtime(
        MockModelProvider([tool_call_response("external.write", {"value": "x"}), "done"]),
        make_agent(
            enabled_tools=frozenset({"external.write"}), tool_permissions=frozenset({"write"})
        ),
    )
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="external.write",
                version="1",
                description="write",
                required_permissions=frozenset({"write"}),
                side_effect=SideEffect.WRITE,
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            ),
            lambda args, ctx: {"ok": True},
        )
    )
    return runtime


async def _paused_for_approval(runtime: AgentRuntime) -> RunState:
    result = await runtime.run(
        RunRequest(agent="test-agent", input="write", tenant_id="t", user_id="u")
    )
    assert result.status == RunStatus.AWAITING_APPROVAL
    return await runtime.status(result.run_id, "t")


def _expire_approval(runtime: AgentRuntime, state: RunState) -> None:
    approval_id = str((state.pause_payload or {})["approval_id"])
    runtime.approvals._items[approval_id].expires_at = utc_now() - timedelta(seconds=1)  # type: ignore[attr-defined]


async def test_pending_approval_within_expiry_keeps_run_paused() -> None:
    runtime = _approval_runtime()
    state = await _paused_for_approval(runtime)

    assert await runtime.expire_paused_runs() == 0
    assert (await runtime.status(state.id, "t")).status == RunStatus.AWAITING_APPROVAL


async def test_expired_approval_times_out_paused_run() -> None:
    runtime = _approval_runtime()
    state = await _paused_for_approval(runtime)
    _expire_approval(runtime, state)

    assert await runtime.expire_paused_runs() == 1

    current = await runtime.status(state.id, "t")
    assert current.status == RunStatus.TIMED_OUT
    assert current.error == "Approval request expired."
    history = await runtime.events.history(state.id)
    assert history[-1].type == "run.failed"
    assert history[-1].data == {"reason": "approval_expired"}
    # A second sweep is a no-op because the run is now terminal.
    assert await runtime.expire_paused_runs() == 0


async def test_missing_approval_times_out_paused_run() -> None:
    runtime = _approval_runtime()
    state = await _paused_for_approval(runtime)
    runtime.approvals._items.clear()  # type: ignore[attr-defined]

    assert await runtime.expire_paused_runs() == 1
    assert (await runtime.status(state.id, "t")).status == RunStatus.TIMED_OUT


async def test_clarification_expires_only_when_timeout_is_configured() -> None:
    runtime = make_runtime()
    state = RunState(
        request=RunRequest(agent="test-agent", input="q", tenant_id="t", user_id="u"),
        status=RunStatus.AWAITING_CLARIFICATION,
        pause_payload={"question": "Which order?"},
        updated_at=utc_now() - timedelta(hours=2),
    )
    await runtime.runs.create(state)

    assert await runtime.expire_paused_runs() == 0
    assert await runtime.expire_paused_runs(clarification_timeout_seconds=3 * 3600) == 0
    assert await runtime.expire_paused_runs(clarification_timeout_seconds=3600) == 1

    current = await runtime.status(state.id, "t")
    assert current.status == RunStatus.TIMED_OUT
    history = await runtime.events.history(state.id)
    assert history[-1].data == {"reason": "clarification_expired"}


class RecordingSweeper:
    def __init__(self, error: Exception | None = None) -> None:
        self.policies: list[RetentionPolicy] = []
        self._error = error

    async def sweep(self, policy: RetentionPolicy) -> RetentionReport:
        self.policies.append(policy)
        if self._error:
            raise self._error
        return RetentionReport(deleted={"runs": 3})


async def test_maintenance_run_once_combines_expiry_artifacts_and_retention() -> None:
    runtime = _approval_runtime()
    state = await _paused_for_approval(runtime)
    _expire_approval(runtime, state)
    artifacts = InMemoryArtifactStore()
    for index in range(3):
        await artifacts.put(
            Artifact(
                tenant_id="t",
                name=f"a{index}",
                media_type="text/plain",
                data=b"x",
                expires_at=utc_now() - timedelta(seconds=1),
            )
        )
    sweeper = RecordingSweeper()
    service = MaintenanceService(
        runtime,
        artifacts,
        MaintenanceSettings(batch_size=2, terminal_run_retention_days=30, audit_retention_days=365),
        sweeper,
    )

    report = await service.run_once()

    assert report.expired_paused_runs == 1
    assert report.purged_artifacts == 3
    assert report.deleted == {"runs": 3}
    assert report.errors == {}
    assert sweeper.policies == [
        RetentionPolicy(
            terminal_runs=timedelta(days=30),
            audit_events=timedelta(days=365),
            batch_size=2,
            max_batches=20,
        )
    ]


async def test_maintenance_task_failure_is_reported_without_stopping_other_tasks() -> None:
    runtime = _approval_runtime()
    state = await _paused_for_approval(runtime)
    _expire_approval(runtime, state)
    service = MaintenanceService(
        runtime,
        InMemoryArtifactStore(),
        MaintenanceSettings(),
        RecordingSweeper(error=ConnectionError("database unavailable")),
    )

    report = await service.run_once()

    assert report.errors == {"retention": "ConnectionError"}
    assert report.expired_paused_runs == 1


async def test_maintenance_can_skip_paused_run_expiry() -> None:
    runtime = _approval_runtime()
    state = await _paused_for_approval(runtime)
    _expire_approval(runtime, state)
    service = MaintenanceService(
        runtime, InMemoryArtifactStore(), MaintenanceSettings(expire_paused_runs=False)
    )

    report = await service.run_once()

    assert report.expired_paused_runs == 0
    assert (await runtime.status(state.id, "t")).status == RunStatus.AWAITING_APPROVAL


async def test_maintenance_background_loop_runs_on_interval_and_stops() -> None:
    service = MaintenanceService(make_runtime(), InMemoryArtifactStore(), MaintenanceSettings())
    sweeps = 0

    async def counted() -> Any:
        nonlocal sweeps
        sweeps += 1

    service.run_once = counted  # type: ignore[method-assign]
    service._settings = MaintenanceSettings.model_construct(  # type: ignore[attr-defined]
        **{**MaintenanceSettings().model_dump(), "interval_seconds": 0.01}
    )
    service.start()
    for _ in range(200):
        if sweeps >= 2:
            break
        await asyncio.sleep(0.005)
    await service.stop()
    stopped_at = sweeps
    await asyncio.sleep(0.03)

    assert stopped_at >= 2
    assert sweeps == stopped_at


async def test_container_starts_background_maintenance_only_when_enabled() -> None:
    disabled = build_container(AppSettings())
    enabled = build_container(AppSettings(maintenance=MaintenanceSettings(enabled=True)))
    assert disabled.maintenance is not None
    assert enabled.maintenance is not None

    for container, expected in ((disabled, False), (enabled, True)):
        await container.astart()
        try:
            task = container.maintenance._task  # type: ignore[union-attr]
            assert (task is not None and not task.done()) is expected
        finally:
            await container.aclose()
        assert container.maintenance._task is None  # type: ignore[union-attr]


def test_maintenance_settings_reject_unsafe_values() -> None:
    with pytest.raises(ValidationError):
        MaintenanceSettings(interval_seconds=1)
    with pytest.raises(ValidationError):
        MaintenanceSettings(terminal_run_retention_days=0)
    with pytest.raises(ValidationError):
        MaintenanceSettings(clarification_timeout_seconds=5)
    assert MaintenanceSettings().terminal_run_retention_days is None
    assert MaintenanceSettings().audit_retention_days is None


def test_cli_parses_maintenance_command() -> None:
    args = build_parser().parse_args(["maintenance", "--config", "a.yaml", "b.yaml"])
    assert args.command == "maintenance"
    assert args.config == ["a.yaml", "b.yaml"]


class FakeDatabase:
    def __init__(self, deleted_per_call: int) -> None:
        self.deleted_per_call = deleted_per_call
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def execute(self, query: str, *arguments: Any) -> str:
        self.statements.append((query, arguments))
        return f"DELETE {self.deleted_per_call}"

    async def fetchrow(self, query: str, *arguments: Any) -> dict[str, int]:
        self.statements.append((query, arguments))
        return {
            "runs": self.deleted_per_call,
            "events": self.deleted_per_call * 4,
            "approvals": 0,
            "tool_executions": 1,
        }


async def test_postgres_sweeper_only_purges_expired_memory_by_default() -> None:
    database = FakeDatabase(deleted_per_call=0)

    report = await PostgresRetentionSweeper(database).sweep(RetentionPolicy())

    assert report.deleted == {"memory": 0}
    assert len(database.statements) == 1
    assert "algen_agent_runtime_memory" in database.statements[0][0]


async def test_postgres_sweeper_batches_until_short_batch_or_limit() -> None:
    database = FakeDatabase(deleted_per_call=10)
    policy = RetentionPolicy(
        terminal_runs=timedelta(days=7),
        audit_events=timedelta(days=90),
        batch_size=10,
        max_batches=3,
    )

    report = await PostgresRetentionSweeper(database).sweep(policy)

    assert report.deleted == {
        "memory": 30,
        "runs": 30,
        "events": 120,
        "approvals": 0,
        "tool_executions": 3,
        "audit_events": 30,
    }
    run_calls = [args for query, args in database.statements if "deleted_runs" in query]
    audit_calls = [args for query, args in database.statements if "audit_events" in query]
    assert run_calls == [(7 * 86_400, 10)] * 3
    assert audit_calls == [(90 * 86_400, 10)] * 3
