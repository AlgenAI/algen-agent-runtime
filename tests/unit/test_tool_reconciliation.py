"""Operator reconciliation, the reconcile hook, compensation and crash-between-writes safety."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from pydantic import ValidationError

from algen_agent_runtime.exceptions.errors import ConflictError, NotFoundError, ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.describe import _tool
from algen_agent_runtime.persistence.memory import InMemoryRunStore
from algen_agent_runtime.persistence.tool_executions import InMemoryToolExecutionStore
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    ReconcileOutcome,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionRecord,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    ToolPolicy,
)

TENANT = "tenant-a"
TOOL = "crm.update"


def write_tool(
    handler: Any,
    *,
    reconcile: Any = None,
    compensation: Any = None,
    reconcile_on_replay: bool = True,
    side_effect: SideEffect = SideEffect.EXTERNAL,
) -> Tool:
    return Tool(
        ToolDefinition(
            name=TOOL,
            version="1",
            description="update an account",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            side_effect=side_effect,
            idempotency=Idempotency.KEYED,
            timeout_seconds=5,
            retry_policy={"max_attempts": 1},
        ),
        handler,
        compensation,
        reconcile=reconcile,
        reconcile_on_replay=reconcile_on_replay,
    )


def agent(**tool_policy: Any) -> Any:
    return make_agent(
        enabled_tools=frozenset({TOOL}),
        approval_policy=ApprovalPolicy(require_for_side_effects=False),
        tool_policy=ToolPolicy(**tool_policy),
    )


def request(tenant: str = TENANT) -> RunRequest:
    return RunRequest(agent="test-agent", input="update a", tenant_id=tenant, user_id="user-1")


def breaking_handler(arguments: dict[str, Any], context: ToolContext) -> Any:
    raise ConnectionError("connection reset after submit")


async def paused_run(
    tool: Tool, *, replies: tuple[Any, ...] = ("done",)
) -> tuple[AgentRuntime, MockModelProvider, RunState]:
    provider = MockModelProvider([tool_call_response(TOOL, {"id": "a"}), *replies])
    runtime = make_runtime(provider, agent(on_indeterminate="pause"))
    runtime.tools.register(tool)
    result = await runtime.run(request())
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    return runtime, provider, await runtime.status(result.run_id, TENANT)


def key_of(state: RunState) -> str:
    return str((state.pause_payload or {})["idempotency_key"])


def last_tool_message(provider: MockModelProvider) -> dict[str, Any]:
    message = next(m for m in reversed(provider.requests[-1].messages) if m.role == Role.TOOL)
    return dict(json.loads(message.text_content))


async def event_types(runtime: AgentRuntime, run_id: str) -> list[str]:
    return [event.type for event in await runtime.events.history(run_id)]


def context(key: str, *, tenant: str = TENANT, run_id: str = "run-1") -> ToolContext:
    return ToolContext(
        run_id=run_id,
        step_id="tool-call-1",
        tenant_id=tenant,
        user_id="user-1",
        permissions=frozenset(),
        idempotency_key=key,
    )


# -- the ledger --------------------------------------------------------------------------------


async def test_store_lists_by_status_run_tool_and_age_within_one_tenant() -> None:
    store = InMemoryToolExecutionStore()
    await store.begin(context("k1", run_id="r1"), TOOL)
    await store.fail("k1", "ConnectionError", indeterminate=True)
    await store.begin(context("k2", run_id="r2"), "other.tool")
    await store.fail("k2", "ConnectionError", indeterminate=True)
    await store.begin(context("k3", run_id="r1"), TOOL)
    await store.complete("k3", ToolResult(value={"ok": True}))
    await store.begin(context("k4", tenant="tenant-b", run_id="r9"), TOOL)
    await store.fail("k4", "ConnectionError", indeterminate=True)

    indeterminate = {ToolExecutionStatus.INDETERMINATE}
    found = await store.find(TENANT, statuses=indeterminate)
    assert [record.idempotency_key for record in found] == ["k1", "k2"]
    assert all(record.created_at and record.updated_at for record in found)
    assert [
        r.idempotency_key for r in await store.find(TENANT, statuses=indeterminate, run_id="r1")
    ] == ["k1"]
    assert [
        r.idempotency_key
        for r in await store.find(TENANT, statuses=indeterminate, tool_name="other.tool")
    ] == ["k2"]
    assert await store.find(TENANT, statuses=indeterminate, min_age=timedelta(hours=1)) == ()
    assert await store.get("k4", TENANT) is None
    assert (await store.get("k4", "tenant-b")) is not None


async def test_store_transition_is_compare_and_set_and_never_crosses_tenants() -> None:
    store = InMemoryToolExecutionStore()
    await store.begin(context("k1"), TOOL)
    await store.fail("k1", "ConnectionError", indeterminate=True)
    record = await store.get("k1", TENANT)
    assert record is not None
    done = record.model_copy(update={"status": ToolExecutionStatus.COMPLETED})
    other_tenant = done.model_copy(update={"tenant_id": "tenant-b"})
    assert (
        await store.transition(other_tenant, expected={ToolExecutionStatus.INDETERMINATE}) is None
    )
    assert await store.transition(done, expected={ToolExecutionStatus.INDETERMINATE}) is not None
    assert await store.transition(done, expected={ToolExecutionStatus.INDETERMINATE}) is None


async def test_late_outcome_of_a_call_never_overwrites_an_operator_decision() -> None:
    store = InMemoryToolExecutionStore()
    await store.begin(context("k1"), TOOL)
    record = await store.get("k1", TENANT)
    assert record is not None
    failed = record.model_copy(update={"status": ToolExecutionStatus.FAILED})
    assert await store.transition(failed, expected={ToolExecutionStatus.STARTED})
    await store.complete("k1", ToolResult(value={"ok": True}))
    await store.fail("k1", "TimeoutError", indeterminate=True)
    current = await store.get("k1", TENANT)
    assert current is not None and current.status == ToolExecutionStatus.FAILED


# -- runs paused on an unknown outcome ---------------------------------------------------------


async def test_default_policy_fails_the_run_and_resolution_is_record_keeping_only() -> None:
    provider = MockModelProvider([tool_call_response(TOOL, {"id": "a"}), "done"])
    runtime = make_runtime(provider, agent())
    runtime.tools.register(write_tool(breaking_handler))
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED
    [record] = await runtime.reconciliation.list(TENANT)
    assert record.status == ToolExecutionStatus.INDETERMINATE

    report = await runtime.reconciliation.resolve(
        TENANT,
        record.idempotency_key,
        status="completed",
        actor_id="ops-1",
        reason="provider dashboard shows the update",
    )
    assert report.outcome == "completed" and report.run_resumed is False
    assert (await runtime.status(result.run_id, TENANT)).status == RunStatus.FAILED
    assert "tool.reconciled" in await event_types(runtime, result.run_id)


async def test_paused_run_resumes_with_the_recorded_result_after_resolution() -> None:
    runtime, provider, state = await paused_run(write_tool(breaking_handler))
    assert state.pause_payload is not None
    assert state.pause_payload["kind"] == "tool_reconciliation"
    assert state.pause_payload["execution_status"] == "indeterminate"
    assert "tool.reconciliation_required" in await event_types(runtime, state.id)
    # The pause names the call, never its arguments.
    assert "id" not in state.pause_payload

    report = await runtime.reconciliation.resolve(
        TENANT,
        key_of(state),
        status="completed",
        actor_id="ops-1",
        reason="CRM shows the owner changed",
        result={"ok": True},
        result_reference="crm-audit-991",
        expected_status="indeterminate",
    )
    assert report.run_resumed is True
    result = await runtime.wait(state.id, TENANT)
    assert result.status == RunStatus.COMPLETED and result.output == "done"
    assert last_tool_message(provider) == {"ok": True}

    record = report.record
    assert record.status == ToolExecutionStatus.COMPLETED
    [decision] = record.resolutions
    assert decision.actor_id == "ops-1" and decision.reason == "CRM shows the owner changed"
    assert decision.previous_status == ToolExecutionStatus.INDETERMINATE
    assert decision.result_reference == "crm-audit-991"
    audits = await runtime.audits.list(TENANT)
    reconciled = [audit for audit in audits if audit.action == "tool.reconcile"]
    assert reconciled[-1].actor_id == "ops-1" and reconciled[-1].outcome == "completed"
    assert reconciled[-1].metadata["reason"] == "CRM shows the owner changed"
    events = await runtime.events.history(state.id)
    [announced] = [event for event in events if event.type == "tool.reconciled"]
    # Content-minimal: no result, no reason, no arguments.
    assert set(announced.data) == {"tool", "idempotency_key", "status", "previous_status", "method"}
    narrative = await runtime.narrative(state.id, TENANT)
    assert narrative.status == "completed"


async def test_paused_run_gets_a_tool_error_when_resolved_failed() -> None:
    runtime, provider, state = await paused_run(write_tool(breaking_handler))
    report = await runtime.reconciliation.resolve(
        TENANT, key_of(state), status="failed", actor_id="ops-1", reason="never arrived"
    )
    assert report.run_resumed is True
    result = await runtime.wait(state.id, TENANT)
    assert result.status == RunStatus.COMPLETED
    assert last_tool_message(provider)["error"]["type"] == "ToolCallNotApplied"


async def test_completed_without_a_result_tells_the_model_it_took_effect() -> None:
    runtime, provider, state = await paused_run(write_tool(breaking_handler))
    await runtime.reconciliation.resolve(
        TENANT,
        key_of(state),
        status="completed",
        actor_id="ops-1",
        reason="seen upstream",
        result_reference="ref-7",
    )
    await runtime.wait(state.id, TENANT)
    message = last_tool_message(provider)
    assert message["status"] == "completed" and message["result_reference"] == "ref-7"


async def test_resolution_checks_the_result_against_the_output_schema() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    with pytest.raises(ToolExecutionError, match="output schema"):
        await runtime.reconciliation.resolve(
            TENANT,
            key_of(state),
            status="completed",
            actor_id="ops-1",
            reason="x",
            result={"unexpected": 1},
        )
    with pytest.raises(ValueError, match="reason"):
        await runtime.reconciliation.resolve(
            TENANT, key_of(state), status="failed", actor_id="ops-1", reason="  "
        )
    with pytest.raises(ValueError, match="result"):
        await runtime.reconciliation.resolve(
            TENANT, key_of(state), status="failed", actor_id="ops-1", reason="x", result={"ok": 1}
        )


async def test_concurrent_resolutions_compare_and_set_so_exactly_one_wins() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    key = key_of(state)
    outcomes = await asyncio.gather(
        runtime.reconciliation.resolve(
            TENANT, key, status="completed", actor_id="ops-1", reason="a", resume=False
        ),
        runtime.reconciliation.resolve(
            TENANT, key, status="failed", actor_id="ops-2", reason="b", resume=False
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    record = await runtime.reconciliation.get(TENANT, key)
    assert len(record.resolutions) == 1
    with pytest.raises(ConflictError, match="cannot be resolved"):
        await runtime.reconciliation.resolve(
            TENANT, key, status="failed", actor_id="ops-3", reason="late"
        )


async def test_expected_status_guards_against_a_change_since_the_operator_looked() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    with pytest.raises(ConflictError, match="not started"):
        await runtime.reconciliation.resolve(
            TENANT,
            key_of(state),
            status="failed",
            actor_id="ops-1",
            reason="x",
            expected_status="started",
        )


async def test_reconciliation_is_tenant_scoped() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    assert await runtime.reconciliation.list("tenant-b") == ()
    with pytest.raises(NotFoundError):
        await runtime.reconciliation.get("tenant-b", key_of(state))
    with pytest.raises(NotFoundError):
        await runtime.reconciliation.resolve(
            "tenant-b", key_of(state), status="failed", actor_id="ops", reason="x"
        )
    with pytest.raises(NotFoundError):
        await runtime.reconciliation.compensate(
            "tenant-b", key_of(state), actor_id="ops", reason="x"
        )
    assert (await runtime.status(state.id, TENANT)).status == RunStatus.AWAITING_RECONCILIATION


async def test_cancelled_paused_run_is_not_resumed_by_resolution() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    await runtime.cancel(state.id, TENANT)
    report = await runtime.reconciliation.resolve(
        TENANT, key_of(state), status="failed", actor_id="ops", reason="x"
    )
    assert report.run_resumed is False
    assert (await runtime.status(state.id, TENANT)).status == RunStatus.CANCELLED


async def test_resume_continues_a_run_left_waiting_on_a_settled_call() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    # The Runtime stopped between settling the call and continuing the run.
    await runtime.reconciliation.resolve(
        TENANT, key_of(state), status="failed", actor_id="ops", reason="x", resume=False
    )
    assert (await runtime.status(state.id, TENANT)).status == RunStatus.AWAITING_RECONCILIATION
    assert await runtime.recover() == 0
    report = await runtime.reconciliation.resume(TENANT, key_of(state), actor_id="ops")
    assert report.run_resumed is True
    assert (await runtime.wait(state.id, TENANT)).status == RunStatus.COMPLETED
    with pytest.raises(ConflictError, match="not waiting"):
        await runtime.reconciliation.resume(TENANT, key_of(state), actor_id="ops")


async def test_read_only_calls_never_pause_the_run() -> None:
    provider = MockModelProvider([tool_call_response(TOOL, {"id": "a"}), "done"])
    runtime = make_runtime(provider, agent(on_indeterminate="pause"))
    runtime.tools.register(write_tool(breaking_handler, side_effect=SideEffect.READ))
    result = await runtime.run(request())
    assert result.status == RunStatus.FAILED


async def test_parallel_batch_records_the_others_and_pauses_on_the_unknown_call() -> None:
    from algen_agent_runtime.types.contracts import (
        FinishReason,
        Message,
        ModelResponse,
        ToolCall,
    )

    calls = (
        ToolCall(id="call-ok", name=TOOL, arguments={"id": "ok"}),
        ToolCall(id="call-bad", name=TOOL, arguments={"id": "bad"}),
    )
    provider = MockModelProvider(
        [
            ModelResponse(
                message=Message.text(Role.ASSISTANT, ""),
                tool_calls=calls,
                finish_reason=FinishReason.TOOL_CALLS,
                model="deterministic",
                provider="mock",
            ),
            "done",
        ]
    )

    def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        if arguments["id"] == "bad":
            raise ConnectionError("reset")
        return {"ok": True}

    runtime = make_runtime(
        provider,
        make_agent(
            planning_strategy="direct",
            enabled_tools=frozenset({TOOL}),
            approval_policy=ApprovalPolicy(require_for_side_effects=False),
            tool_policy=ToolPolicy(parallel_calls=True, on_indeterminate="pause"),
        ),
    )
    runtime.tools.register(write_tool(handler))
    result = await runtime.run(request())
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    state = await runtime.status(result.run_id, TENANT)
    assert state.completed_tool_call_ids == {"call-ok"}
    assert (state.pause_payload or {})["tool_call_id"] == "call-bad"
    await runtime.reconciliation.resolve(
        TENANT, key_of(state), status="failed", actor_id="ops", reason="x"
    )
    assert (await runtime.wait(state.id, TENANT)).status == RunStatus.COMPLETED


async def test_started_call_cannot_be_settled_until_stale() -> None:
    store = InMemoryToolExecutionStore()
    runtime = make_runtime(MockModelProvider(["x"]), agent())
    runtime.tool_executor = ToolExecutor(runtime.tools, runtime.policies, store)
    await runtime.runs.create(RunState(request=request(), id="run-1"))
    await store.begin(context("run-1:call-1"), TOOL)
    assert [r.status for r in await runtime.reconciliation.list(TENANT)] == ["started"]
    with pytest.raises(ConflictError, match="may still be running"):
        await runtime.reconciliation.resolve(
            TENANT, "run-1:call-1", status="failed", actor_id="ops", reason="x"
        )
    runtime.reconciliation.stale_started_after = timedelta(0)
    report = await runtime.reconciliation.resolve(
        TENANT, "run-1:call-1", status="failed", actor_id="ops", reason="x"
    )
    assert report.record.status == ToolExecutionStatus.FAILED


# -- compensation ------------------------------------------------------------------------------


async def completed_call(compensation: Any) -> tuple[AgentRuntime, str]:
    provider = MockModelProvider([tool_call_response(TOOL, {"id": "a"}), "done"])
    runtime = make_runtime(provider, agent())
    runtime.tools.register(write_tool(lambda a, c: {"ok": True}, compensation=compensation))
    result = await runtime.run(request())
    assert result.status == RunStatus.COMPLETED
    [record] = await runtime.reconciliation.list(TENANT, statuses={ToolExecutionStatus.COMPLETED})
    return runtime, record.idempotency_key


async def test_compensation_runs_once_only_when_asked_and_is_audited() -> None:
    calls: list[tuple[Any, str]] = []

    async def undo(result: ToolResult, context: ToolContext) -> None:
        await asyncio.sleep(0)
        calls.append((result.value, context.idempotency_key))

    runtime, key = await completed_call(undo)
    # Never by itself: the run completed and nothing asked for it.
    assert calls == []
    outcomes = await asyncio.gather(
        runtime.reconciliation.compensate(TENANT, key, actor_id="ops-1", reason="customer asked"),
        runtime.reconciliation.compensate(TENANT, key, actor_id="ops-2", reason="duplicate"),
        return_exceptions=True,
    )
    assert calls == [({"ok": True}, key)]
    reports = [item for item in outcomes if not isinstance(item, BaseException)]
    assert [report.outcome for report in reports] == ["compensated"]
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    with pytest.raises(ConflictError, match="cannot be compensated"):
        await runtime.reconciliation.compensate(TENANT, key, actor_id="ops-1", reason="again")
    assert len(calls) == 1

    record = await runtime.reconciliation.get(TENANT, key)
    assert record.status == ToolExecutionStatus.COMPENSATED
    assert [d.status for d in record.resolutions] == ["compensating", "compensated"]
    audits = [a for a in await runtime.audits.list(TENANT) if a.action == "tool.compensation"]
    assert [a.outcome for a in audits] == ["started", "completed"]
    assert {a.actor_id for a in audits} == {"ops-1"}
    types = await event_types(runtime, record.run_id)
    assert "tool.compensation.started" in types and "tool.compensation.completed" in types


async def test_failed_compensation_is_recorded_and_may_be_requested_again() -> None:
    attempts: list[int] = []

    def undo(result: ToolResult, context: ToolContext) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("upstream 503")

    runtime, key = await completed_call(undo)
    first = await runtime.reconciliation.compensate(TENANT, key, actor_id="ops", reason="x")
    assert first.outcome == "compensation_failed"
    assert first.record.error == "RuntimeError"
    assert [
        r.idempotency_key
        for r in await runtime.reconciliation.list(
            TENANT, statuses={ToolExecutionStatus.COMPENSATION_FAILED}
        )
    ] == [key]
    second = await runtime.reconciliation.compensate(TENANT, key, actor_id="ops", reason="retry")
    assert second.outcome == "compensated" and len(attempts) == 2


async def test_compensation_left_running_by_a_crash_never_runs_again_by_itself() -> None:
    calls: list[int] = []
    runtime, key = await completed_call(lambda result, context: calls.append(1))
    record = await runtime.reconciliation.get(TENANT, key)
    # The process stopped after claiming the compensation, before the handler finished.
    claimed = record.model_copy(update={"status": ToolExecutionStatus.COMPENSATING})
    assert await runtime.reconciliation.store.transition(
        claimed, expected={ToolExecutionStatus.COMPLETED}
    )
    with pytest.raises(ConflictError):
        await runtime.reconciliation.compensate(TENANT, key, actor_id="ops", reason="x")
    report = await runtime.reconciliation.resolve(
        TENANT, key, status="compensated", actor_id="ops", reason="refund visible upstream"
    )
    assert report.record.status == ToolExecutionStatus.COMPENSATED and calls == []


async def test_tool_without_compensation_or_reconcile_is_refused() -> None:
    runtime, key = await completed_call(None)
    with pytest.raises(ConflictError, match="no compensation"):
        await runtime.reconciliation.compensate(TENANT, key, actor_id="ops", reason="x")


async def test_compensating_an_indeterminate_call_resumes_its_paused_run() -> None:
    seen: list[ToolResult] = []
    tool = write_tool(breaking_handler, compensation=lambda result, context: seen.append(result))
    runtime, provider, state = await paused_run(tool)
    report = await runtime.reconciliation.compensate(
        TENANT, key_of(state), actor_id="ops", reason="void the half-sent order"
    )
    assert report.run_resumed is True
    assert seen[0].value is None and seen[0].metadata == {"execution_status": "indeterminate"}
    assert (await runtime.wait(state.id, TENANT)).status == RunStatus.COMPLETED
    assert last_tool_message(provider)["error"]["type"] == "ToolCallCompensated"


# -- the reconcile hook ------------------------------------------------------------------------


async def test_auto_reconcile_records_what_the_external_system_reports() -> None:
    answers = iter(
        [
            ReconcileOutcome(status="unknown"),
            {
                "status": "completed",
                "value": {"ok": True},
                "result_reference": "crm-1",
                "detail": "found",
            },
        ]
    )
    asked: list[str] = []

    def reconcile(record: ToolExecutionRecord, context: ToolContext) -> Any:
        asked.append(context.idempotency_key)
        assert not context.credentials
        return next(answers)

    tool = write_tool(breaking_handler, reconcile=reconcile, reconcile_on_replay=False)
    runtime, provider, state = await paused_run(tool)
    unknown = await runtime.reconciliation.auto_reconcile(TENANT, key_of(state), actor_id="ops")
    assert unknown.outcome == "unknown"
    assert unknown.record.status == ToolExecutionStatus.INDETERMINATE
    done = await runtime.reconciliation.auto_reconcile(TENANT, key_of(state), actor_id="ops")
    assert done.outcome == "completed" and done.run_resumed is True
    assert done.record.resolutions[-1].action == "reconcile"
    assert done.record.resolutions[-1].reason == "found"
    assert asked == [key_of(state), key_of(state)]
    await runtime.wait(state.id, TENANT)
    assert last_tool_message(provider) == {"ok": True}
    outcomes = [
        a.outcome for a in await runtime.audits.list(TENANT) if a.action == "tool.reconcile"
    ]
    assert outcomes == ["unknown", "completed"]


async def test_auto_reconcile_reports_failed_and_hook_errors() -> None:
    def reconcile(record: ToolExecutionRecord, context: ToolContext) -> Any:
        raise TimeoutError("crm down")

    runtime, _, state = await paused_run(write_tool(breaking_handler, reconcile=reconcile))
    with pytest.raises(ToolExecutionError, match="reconcile hook"):
        await runtime.reconciliation.auto_reconcile(TENANT, key_of(state), actor_id="ops")
    assert (await runtime.reconciliation.get(TENANT, key_of(state))).status == "indeterminate"

    runtime.tools.get(TOOL).reconcile = lambda r, c: ReconcileOutcome(status="failed")
    report = await runtime.reconciliation.auto_reconcile(TENANT, key_of(state), actor_id="ops")
    assert report.outcome == "failed" and report.run_resumed is True


async def test_auto_reconcile_requires_a_hook() -> None:
    runtime, _, state = await paused_run(write_tool(breaking_handler))
    with pytest.raises(ConflictError, match="no reconcile hook"):
        await runtime.reconciliation.auto_reconcile(TENANT, key_of(state), actor_id="ops")


def shared_runtime(
    provider: MockModelProvider,
    tool: Tool,
    runs: Any,
    executions: InMemoryToolExecutionStore,
    **tool_policy: Any,
) -> AgentRuntime:
    """A fresh Runtime over existing stores: the process after a restart."""
    runtime = make_runtime(provider, agent(**tool_policy))
    runtime.runs = runs
    runtime.tool_executor = ToolExecutor(runtime.tools, runtime.policies, executions)
    runtime.tools.register(tool)
    return runtime


class _ProcessDied(BaseException):
    """Stands in for the process stopping: nothing after it runs."""


class CrashableLedger(InMemoryToolExecutionStore):
    """Dies instead of recording a call's failure, as a process killed mid-call would."""

    die_on_fail = False

    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None:
        if self.die_on_fail:
            raise _ProcessDied
        await super().fail(idempotency_key, error, indeterminate=indeterminate)


async def interrupted_run(runs: InMemoryRunStore, executions: CrashableLedger) -> RunState:
    """A run checkpointed mid-call, with its reservation still ``started``: a crash."""
    first = shared_runtime(
        MockModelProvider([tool_call_response(TOOL, {"id": "a"})]),
        write_tool(breaking_handler),
        runs,
        executions,
    )
    executions.die_on_fail = True
    state = await first.start(request())
    with pytest.raises(_ProcessDied):
        await first.wait(state.id, TENANT)
    executions.die_on_fail = False
    current = await runs.get(state.id, TENANT)
    assert current is not None and current.status == RunStatus.INVOKING_TOOL
    [record] = await executions.find(TENANT, statuses={ToolExecutionStatus.STARTED})
    assert record.run_id == state.id
    return current


async def test_reconcile_on_replay_settles_a_crashed_call_without_running_it_again() -> None:
    runs, executions = InMemoryRunStore(), CrashableLedger()
    state = await interrupted_run(runs, executions)
    ran: list[int] = []

    def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        ran.append(1)
        return {"ok": True}

    tool = write_tool(
        handler,
        reconcile=lambda record, context: ReconcileOutcome(status="completed", value={"ok": True}),
    )
    provider = MockModelProvider(["done"])
    runtime = shared_runtime(provider, tool, runs, executions)
    assert await runtime.recover() == 1
    result = await runtime.wait(state.id, TENANT)
    assert result.status == RunStatus.COMPLETED and ran == []
    assert last_tool_message(provider) == {"ok": True}
    record = await executions.get(f"{state.id}:{state.pending_tool_calls[0].id}", TENANT)
    assert record is not None and record.status == ToolExecutionStatus.COMPLETED
    assert record.resolutions[-1].actor_id == "runtime"
    events = await runtime.events.history(state.id)
    assert [e.data["method"] for e in events if e.type == "tool.reconciled"] == ["replay"]


async def test_replay_without_a_conclusive_reconcile_pauses_for_an_operator() -> None:
    runs, executions = InMemoryRunStore(), CrashableLedger()
    state = await interrupted_run(runs, executions)
    tool = write_tool(
        breaking_handler, reconcile=lambda record, context: ReconcileOutcome(status="unknown")
    )
    runtime = shared_runtime(
        MockModelProvider(["done"]), tool, runs, executions, on_indeterminate="pause"
    )
    assert await runtime.recover() == 1
    result = await runtime.wait(state.id, TENANT)
    assert result.status == RunStatus.AWAITING_RECONCILIATION
    paused = await runtime.status(state.id, TENANT)
    assert (paused.pause_payload or {})["execution_status"] == "started"


async def test_reconcile_on_replay_can_be_turned_off() -> None:
    runs, executions = InMemoryRunStore(), CrashableLedger()
    state = await interrupted_run(runs, executions)
    asked: list[int] = []
    tool = write_tool(
        breaking_handler,
        reconcile=lambda record, context: asked.append(1) or ReconcileOutcome(status="failed"),
        reconcile_on_replay=False,
    )
    runtime = shared_runtime(MockModelProvider(["done"]), tool, runs, executions)
    await runtime.recover()
    result = await runtime.wait(state.id, TENANT)
    assert result.status == RunStatus.FAILED and "manual reconciliation" in (result.error or "")
    assert asked == []


# -- crash between the ledger write and the run checkpoint -------------------------------------


class CrashingRunStore(InMemoryRunStore):
    """Dies on the checkpoint that records a tool call as answered."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = True

    async def save(self, state: RunState, expected_version: int) -> None:
        if (
            self.armed
            and state.status == RunStatus.BUILDING_CONTEXT
            and any(message.role == Role.TOOL for message in state.messages)
        ):
            self.armed = False
            raise _ProcessDied
        await super().save(state, expected_version)


async def test_crash_between_ledger_completion_and_checkpoint_replays_without_rerunning() -> None:
    runs, executions = CrashingRunStore(), InMemoryToolExecutionStore()
    ran: list[str] = []

    def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        ran.append(context.idempotency_key)
        return {"ok": True}

    first = shared_runtime(
        MockModelProvider([tool_call_response(TOOL, {"id": "a"})]),
        write_tool(handler),
        runs,
        executions,
    )
    state = await first.start(request())
    with pytest.raises(_ProcessDied):
        await first.wait(state.id, TENANT)
    # The ledger says completed; the checkpoint still has the call pending.
    checkpoint = await runs.get(state.id, TENANT)
    assert checkpoint is not None and checkpoint.status == RunStatus.INVOKING_TOOL
    assert checkpoint.completed_tool_call_ids == set()
    [record] = await executions.find(TENANT, statuses={ToolExecutionStatus.COMPLETED})

    provider = MockModelProvider(["done"])
    second = shared_runtime(provider, write_tool(handler), runs, executions)
    assert await second.recover() == 1
    result = await second.wait(state.id, TENANT)
    assert result.status == RunStatus.COMPLETED and result.output == "done"
    assert ran == [record.idempotency_key]
    assert last_tool_message(provider) == {"ok": True}
    final = await runs.get(state.id, TENANT)
    assert final is not None
    assert sum(message.role == Role.TOOL for message in final.messages) == 1


# -- configuration -----------------------------------------------------------------------------


def test_on_indeterminate_is_validated_and_describe_shows_the_hooks() -> None:
    assert ToolPolicy().on_indeterminate == "on_error"
    with pytest.raises(ValidationError):
        ToolPolicy(on_indeterminate="retry")
    described = _tool(
        write_tool(breaking_handler, reconcile=lambda r, c: None, compensation=lambda r, c: None),
        "test",
        (),
    )
    assert described.reconcile is True and described.compensation is True
    plain = _tool(write_tool(breaking_handler), "test", ())
    assert plain.reconcile is False and plain.compensation is False
