"""Operator reconciliation of tool calls whose outcome is unknown, and explicit compensation.

A side-effecting call that fails without a clear outcome is recorded ``indeterminate``; one cut
short by a crash stays ``started``. :class:`ToolReconciliation` lets an operator list those
calls for a tenant and settle each one:

- ``resolve``: state the outcome (``completed``, optionally with the result or an external
  reference, or ``failed``);
- ``auto_reconcile``: ask the tool's ``reconcile`` hook, which queries the external system by
  idempotency key;
- ``compensate``: run the tool's ``compensation`` handler for a completed or indeterminate call;
- ``resume``: continue a run that still waits for a call already settled.

Every decision is a compare-and-set on the ledger status, so of two concurrent decisions exactly
one wins, and it is kept in the record (who, why, when), audited, and announced on the run's
event stream without tool arguments or results. A run paused in ``awaiting_reconciliation`` on
the call continues: the model gets the recorded result, or a tool error when the call did not
take effect or was undone. A run that already ended is not reopened; settling its call is
record-keeping only.

Compensation runs only when an operator asks for it. The ledger moves to ``compensating`` before
the handler runs, so it can never run twice for one request, nor again by itself after a crash:
a call left ``compensating`` is settled by an operator (``compensated`` or
``compensation_failed``), and only ``compensation_failed`` may be compensated again.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Collection, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.events.contracts import AuditEvent, EventType
from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PolicyDeniedError,
    ToolExecutionError,
)
from algen_agent_runtime.planning.contracts import ActionType, PlannedAction
from algen_agent_runtime.security.redaction import redact
from algen_agent_runtime.tools.contracts import (
    ReconcilableToolExecutionStore,
    Tool,
    ToolContext,
    ToolExecutionRecord,
    ToolExecutionResolution,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.types.contracts import (
    Message,
    Role,
    RunState,
    RunStatus,
    TextBlock,
    utc_now,
)

if TYPE_CHECKING:
    from algen_agent_runtime.runtime.runtime import AgentRuntime

Status = ToolExecutionStatus

# What an operator lists by default: calls whose outcome nobody knows yet.
DEFAULT_RECONCILIATION_STATUSES: frozenset[ToolExecutionStatus] = frozenset(
    {Status.INDETERMINATE, Status.STARTED}
)
_UNKNOWN_OUTCOME = frozenset({Status.STARTED, Status.INDETERMINATE})
_COMPENSABLE = frozenset({Status.COMPLETED, Status.INDETERMINATE, Status.COMPENSATION_FAILED})
# Settled outcomes a waiting run can continue from.
_SETTLED = frozenset({Status.COMPLETED, Status.FAILED, Status.COMPENSATED})
_RESOLVE_FROM: dict[ToolExecutionStatus, frozenset[ToolExecutionStatus]] = {
    Status.COMPLETED: _UNKNOWN_OUTCOME,
    Status.FAILED: _UNKNOWN_OUTCOME,
    Status.COMPENSATED: frozenset({Status.COMPENSATING}),
    Status.COMPENSATION_FAILED: frozenset({Status.COMPENSATING}),
}
RUNTIME_ACTOR = "runtime"


class ReconciliationReport(BaseModel):
    """What one reconciliation request did."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    record: ToolExecutionRecord = Field(description="The ledger record after the request.")
    outcome: Literal[
        "completed", "failed", "unknown", "compensated", "compensation_failed", "resumed"
    ] = Field(description="What was decided; ``unknown`` leaves the record as it was.")
    run_resumed: bool = Field(
        default=False,
        description="Whether a run paused in ``awaiting_reconciliation`` on this call continued.",
    )


class ToolReconciliation:
    """Tenant-scoped reconciliation, resolution and compensation of recorded tool calls.

    ``stale_started_after`` is how long a ``started`` record must have been unchanged before it
    may be settled: younger ones may belong to a call still running.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        *,
        stale_started_after: timedelta = timedelta(minutes=10),
    ) -> None:
        self._runtime = runtime
        self.stale_started_after = stale_started_after

    @property
    def store(self) -> ReconcilableToolExecutionStore:
        store = self._runtime.tool_executor.executions
        if not isinstance(store, ReconcilableToolExecutionStore):
            raise ConfigurationError(
                "the tool-execution store does not support reconciliation "
                "(get, find and transition)"
            )
        return store

    async def list(
        self,
        tenant_id: str,
        *,
        statuses: Collection[ToolExecutionStatus] = DEFAULT_RECONCILIATION_STATUSES,
        run_id: str | None = None,
        tool_name: str | None = None,
        min_age: timedelta | None = None,
        limit: int = 100,
    ) -> Sequence[ToolExecutionRecord]:
        """A tenant's calls in ``statuses``, least recently changed first."""
        if not statuses:
            raise ValueError("at least one status is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return await self.store.find(
            tenant_id,
            statuses=frozenset(statuses),
            run_id=run_id,
            tool_name=tool_name,
            min_age=min_age,
            limit=limit,
        )

    async def get(self, tenant_id: str, idempotency_key: str) -> ToolExecutionRecord:
        record = await self.store.get(idempotency_key, tenant_id)
        if record is None:
            raise NotFoundError(f"tool execution {idempotency_key!r} not found")
        return record

    async def resolve(
        self,
        tenant_id: str,
        idempotency_key: str,
        *,
        status: ToolExecutionStatus | str,
        actor_id: str,
        reason: str,
        result: Any = None,
        result_reference: str | None = None,
        expected_status: ToolExecutionStatus | str | None = None,
        resume: bool = True,
    ) -> ReconciliationReport:
        """Record an operator's finding about a call.

        ``completed`` or ``failed`` settle a ``started`` or ``indeterminate`` call; ``result``
        (checked against the tool's output schema when the tool is registered) and
        ``result_reference`` describe a completed one. ``compensated`` or
        ``compensation_failed`` settle a call left ``compensating``. ``expected_status``
        guards against a change since the operator looked.
        """
        target = Status(status)
        if target not in _RESOLVE_FROM:
            raise ValueError(
                "status must be completed, failed, compensated, or compensation_failed"
            )
        if result is not None and target != Status.COMPLETED:
            raise ValueError("only a completed call can carry a result")
        reason = _reason(reason, required=True)
        current = await self._current(tenant_id, idempotency_key, expected_status)
        if current.status not in _RESOLVE_FROM[target]:
            raise ConflictError(
                f"a {current.status.value} tool execution cannot be resolved as {target.value}"
            )
        self._require_stale(current)
        recorded: ToolResult | None = current.result
        if target == Status.COMPLETED:
            if result is not None:
                result = self._checked_result(current.tool_name, result)
            metadata: dict[str, Any] = {"reconciled": True}
            if result_reference:
                metadata["result_reference"] = result_reference
            recorded = ToolResult(value=result, metadata=metadata)
        stored = await self._decide(
            current,
            action="resolve",
            status=target,
            actor_id=actor_id,
            reason=reason,
            result_reference=result_reference,
            result=recorded,
            error=None if target in {Status.COMPLETED, Status.COMPENSATED} else _ERRORS[target],
        )
        await self._announce(stored, current.status, actor_id, reason, method="operator")
        resumed = resume and await self._continue_run(stored)
        return ReconciliationReport(record=stored, outcome=_OUTCOMES[target], run_resumed=resumed)

    async def auto_reconcile(
        self,
        tenant_id: str,
        idempotency_key: str,
        *,
        actor_id: str,
        reason: str = "",
        resume: bool = True,
    ) -> ReconciliationReport:
        """Ask the tool's ``reconcile`` hook whether the call took effect, and record it.

        ``unknown`` changes nothing. A hook that raises or times out is audited and reported as
        an error; the record stays as it was.
        """
        reason = _reason(reason)
        current = await self._current(tenant_id, idempotency_key, None)
        if current.status not in _UNKNOWN_OUTCOME:
            raise ConflictError(f"a {current.status.value} tool execution has a known outcome")
        self._require_stale(current)
        tool = self._tool(current.tool_name)
        if tool.reconcile is None:
            raise ConflictError(f"tool {current.tool_name} has no reconcile hook")
        context = await self._context(current, actor_id)
        try:
            async with asyncio.timeout(tool.definition.timeout_seconds):
                outcome = await tool.reconcile_call(current, context)
        except Exception as exc:
            await self._audit(
                current,
                "tool.reconcile",
                "error",
                actor_id,
                {"method": "tool", "reason": reason, "error_type": type(exc).__name__},
            )
            raise ToolExecutionError(
                f"reconcile hook for {current.tool_name} failed: {type(exc).__name__}"
            ) from exc
        note = reason or (outcome.detail or "")
        if outcome.status == "unknown":
            await self._audit(
                current,
                "tool.reconcile",
                "unknown",
                actor_id,
                {"method": "tool", "reason": note},
            )
            return ReconciliationReport(record=current, outcome="unknown")
        if outcome.status == "completed":
            value = await self._runtime.tool_executor._checked_output(
                tool.definition.name, tool.definition, outcome.value, context, None
            )
            metadata: dict[str, Any] = {"reconciled": True}
            if outcome.result_reference:
                metadata["result_reference"] = outcome.result_reference
            target, recorded = Status.COMPLETED, ToolResult(value=value, metadata=metadata)
        else:
            target, recorded = Status.FAILED, None
        stored = await self._decide(
            current,
            action="reconcile",
            status=target,
            actor_id=actor_id,
            reason=note,
            result_reference=outcome.result_reference,
            result=recorded,
            error=None if recorded is not None else _ERRORS[Status.FAILED],
        )
        await self._announce(stored, current.status, actor_id, note, method="tool")
        resumed = resume and await self._continue_run(stored)
        return ReconciliationReport(record=stored, outcome=_OUTCOMES[target], run_resumed=resumed)

    async def compensate(
        self,
        tenant_id: str,
        idempotency_key: str,
        *,
        actor_id: str,
        reason: str,
        expected_status: ToolExecutionStatus | str | None = None,
        resume: bool = True,
    ) -> ReconciliationReport:
        """Undo a completed or indeterminate call with the tool's ``compensation`` handler.

        The handler gets the recorded result, or, for an indeterminate call, an empty result
        whose metadata names that status, and the call's context without caller credentials.
        It runs at most once per request; a failed compensation may be requested again.
        """
        reason = _reason(reason, required=True)
        current = await self._current(tenant_id, idempotency_key, expected_status)
        if current.status not in _COMPENSABLE:
            raise ConflictError(f"a {current.status.value} tool execution cannot be compensated")
        tool = self._tool(current.tool_name)
        if tool.compensation is None:
            raise ConflictError(f"tool {current.tool_name} has no compensation")
        context = await self._context(current, actor_id)
        claimed = await self._decide(
            current,
            action="compensate",
            status=Status.COMPENSATING,
            actor_id=actor_id,
            reason=reason,
            result_reference=None,
            result=current.result,
            error=current.error,
        )
        details = {"reason": reason, "previous_status": current.status.value}
        await self._audit(claimed, "tool.compensation", "started", actor_id, details)
        await self._event(claimed, "tool.compensation.started", self._event_data(claimed))
        result = current.result or ToolResult(
            value=None, metadata={"execution_status": current.status.value}
        )
        try:
            async with asyncio.timeout(tool.definition.timeout_seconds):
                await tool.compensate(result, context)
        except Exception as exc:
            failed = await self._decide(
                claimed,
                action="compensate",
                status=Status.COMPENSATION_FAILED,
                actor_id=actor_id,
                reason=reason,
                result_reference=None,
                result=claimed.result,
                error=type(exc).__name__,
            )
            await self._audit(
                failed,
                "tool.compensation",
                "failed",
                actor_id,
                {**details, "error_type": type(exc).__name__},
            )
            await self._event(
                failed,
                "tool.compensation.failed",
                {**self._event_data(failed), "error_type": type(exc).__name__},
            )
            return ReconciliationReport(record=failed, outcome="compensation_failed")
        done = await self._decide(
            claimed,
            action="compensate",
            status=Status.COMPENSATED,
            actor_id=actor_id,
            reason=reason,
            result_reference=None,
            result=claimed.result,
            error=None,
        )
        await self._audit(done, "tool.compensation", "completed", actor_id, details)
        await self._event(done, "tool.compensation.completed", self._event_data(done))
        resumed = resume and await self._continue_run(done)
        return ReconciliationReport(record=done, outcome="compensated", run_resumed=resumed)

    async def resume(
        self, tenant_id: str, idempotency_key: str, *, actor_id: str
    ) -> ReconciliationReport:
        """Continue a run still paused on a call that is already settled.

        For when the Runtime stopped between settling a call and continuing its run.
        """
        current = await self._current(tenant_id, idempotency_key, None)
        if current.status not in _SETTLED:
            raise ConflictError(f"a {current.status.value} tool execution is not settled")
        if not await self._continue_run(current):
            raise ConflictError("the run is not waiting for this tool execution")
        await self._audit(current, "tool.reconcile", "resumed", actor_id, {"method": "resume"})
        return ReconciliationReport(record=current, outcome="resumed", run_resumed=True)

    # -- internals -------------------------------------------------------------------------

    async def _current(
        self,
        tenant_id: str,
        idempotency_key: str,
        expected_status: ToolExecutionStatus | str | None,
    ) -> ToolExecutionRecord:
        current = await self.get(tenant_id, idempotency_key)
        if expected_status is not None and current.status != Status(expected_status):
            raise ConflictError(
                f"tool execution is {current.status.value}, not {Status(expected_status).value}"
            )
        return current

    def _require_stale(self, record: ToolExecutionRecord) -> None:
        if record.status != Status.STARTED or record.updated_at is None:
            return
        if utc_now() - record.updated_at < self.stale_started_after:
            raise ConflictError(
                "the tool execution started recently and may still be running; "
                "wait until it is stale or indeterminate"
            )

    def _tool(self, name: str) -> Tool:
        try:
            return self._runtime.tools.get(name)
        except NotFoundError as exc:
            raise ConflictError(f"tool {name} is not registered") from exc

    def _checked_result(self, tool_name: str, value: Any) -> Any:
        try:
            definition = self._runtime.tools.get(tool_name).definition
        except NotFoundError:
            return value
        try:
            jsonschema.validate(value, definition.output_schema)
        except jsonschema.ValidationError as exc:
            raise ToolExecutionError(
                f"result does not fit the output schema of {tool_name}: {exc.message}"
            ) from exc
        if len(json.dumps(value, default=str).encode()) > definition.max_result_bytes:
            raise ToolExecutionError(f"result exceeds the size limit of {tool_name}")
        return value

    async def _decide(
        self,
        current: ToolExecutionRecord,
        *,
        action: Literal["resolve", "reconcile", "compensate"],
        status: ToolExecutionStatus,
        actor_id: str,
        reason: str,
        result_reference: str | None,
        result: ToolResult | None,
        error: str | None,
    ) -> ToolExecutionRecord:
        decision = ToolExecutionResolution(
            action=action,
            previous_status=current.status,
            status=status,
            actor_id=actor_id,
            reason=reason,
            result_reference=result_reference,
        )
        stored = await self.store.transition(
            current.model_copy(
                update={
                    "status": status,
                    "result": result,
                    "error": error,
                    "resolutions": (*current.resolutions, decision),
                }
            ),
            expected={current.status},
        )
        if stored is None:
            raise ConflictError("the tool execution changed concurrently; read it again")
        return stored

    async def _context(self, record: ToolExecutionRecord, actor_id: str) -> ToolContext:
        """The call's context for a hook run by an operator: no caller credentials."""
        state = await self._runtime.runs.get(record.run_id, record.tenant_id)
        user_id, permissions = actor_id, frozenset[str]()
        if state is not None:
            user_id = state.request.user_id
            try:
                agent = self._runtime.agents.get(state.request.agent, state.request.agent_version)
                permissions = agent.tool_permissions
            except NotFoundError:
                pass
        return ToolContext(
            run_id=record.run_id,
            step_id=record.step_id,
            tenant_id=record.tenant_id,
            user_id=user_id,
            permissions=permissions,
            idempotency_key=record.idempotency_key,
        )

    @staticmethod
    def _event_data(record: ToolExecutionRecord) -> dict[str, Any]:
        return {
            "tool": record.tool_name,
            "idempotency_key": record.idempotency_key,
            "status": record.status.value,
        }

    async def _announce(
        self,
        record: ToolExecutionRecord,
        previous: ToolExecutionStatus,
        actor_id: str,
        reason: str,
        *,
        method: Literal["operator", "tool"],
    ) -> None:
        details: dict[str, Any] = {
            "method": method,
            "previous_status": previous.value,
            "reason": reason,
        }
        reference = record.resolutions[-1].result_reference if record.resolutions else None
        if reference:
            details["result_reference"] = reference
        await self._audit(record, "tool.reconcile", record.status.value, actor_id, details)
        await self._event(
            record,
            "tool.reconciled",
            {**self._event_data(record), "previous_status": previous.value, "method": method},
        )

    async def _audit(
        self,
        record: ToolExecutionRecord,
        action: str,
        outcome: str,
        actor_id: str,
        details: dict[str, Any],
    ) -> None:
        await self._runtime.audits.append(
            AuditEvent(
                action=action,
                outcome=outcome,
                tenant_id=record.tenant_id,
                actor_id=actor_id,
                resource_id=record.tool_name,
                metadata=redact(
                    {
                        "run_id": record.run_id,
                        "step_id": record.step_id,
                        "idempotency_key": record.idempotency_key,
                        **details,
                    }
                ),
            )
        )

    async def _event(
        self, record: ToolExecutionRecord, event_type: EventType, data: dict[str, Any]
    ) -> None:
        state = await self._runtime.runs.get(record.run_id, record.tenant_id)
        if state is not None:
            await self._runtime._emit(state, event_type, data, record.step_id)

    async def _continue_run(self, record: ToolExecutionRecord) -> bool:
        """Continue the run paused on this call, if it is; True when it continued."""
        runtime = self._runtime
        state = await runtime.runs.get(record.run_id, record.tenant_id)
        if state is None or state.status != RunStatus.AWAITING_RECONCILIATION:
            return False
        pause = state.pause_payload or {}
        if pause.get("idempotency_key") != record.idempotency_key:
            return False
        call_id = str(pause.get("tool_call_id", ""))
        settled = "completed" if record.status == Status.COMPLETED else "failed"
        text = await self._tool_message(state, record, call_id)
        state.completed_tool_call_ids.add(call_id)
        state.pending_tool_calls = [call for call in state.pending_tool_calls if call.id != call_id]
        state.append_message(
            Message(
                role=Role.TOOL,
                name=record.tool_name,
                tool_call_id=call_id,
                content=(TextBlock(text=text),),
            )
        )
        state.output_text = None
        state.summary = state.summary.model_copy(
            update={"tool_calls": state.summary.tool_calls + 1}
        )
        runtime._restore_deadline_after_pause(state)
        state.pause_payload = None
        try:
            await runtime._transition(state, RunStatus.BUILDING_CONTEXT)
        except ConflictError:
            # Cancelled, timed out or continued by someone else meanwhile.
            return False
        await runtime._emit(
            state,
            "tool.completed",
            {"tool": record.tool_name, "status": settled, "reconciled": True},
            record.step_id,
        )
        await runtime._schedule(state.id, state.request.tenant_id)
        return True

    async def _tool_message(
        self, state: RunState, record: ToolExecutionRecord, call_id: str
    ) -> str:
        """What the model reads as the call's result once it is settled."""
        if record.status == Status.COMPENSATED:
            return json.dumps(
                {
                    "error": {
                        "type": "ToolCallCompensated",
                        "message": "This call's effect was undone by an operator.",
                    }
                }
            )
        if record.status != Status.COMPLETED or record.result is None:
            return json.dumps(
                {
                    "error": {
                        "type": "ToolCallNotApplied",
                        "message": "This call did not take effect.",
                    }
                }
            )
        if record.result.value is None:
            confirmed: dict[str, Any] = {
                "status": "completed",
                "message": "This call took effect; its result was not recorded.",
            }
            reference = record.result.metadata.get("result_reference")
            if reference:
                confirmed["result_reference"] = reference
            return json.dumps(confirmed)
        text = json.dumps(record.result.value, default=str)
        agent = self._runtime.agents.get(state.request.agent, state.request.agent_version)
        action = PlannedAction(
            id=f"tool-{call_id}",
            type=ActionType.TOOL,
            description="reconciled tool call",
            tool_name=record.tool_name,
        )
        try:
            # A reconciled result is tool output like any other: screened before the model.
            return await self._runtime._screen_tool_result(state, agent, action, text)
        except PolicyDeniedError as exc:
            return json.dumps({"error": {"type": "PolicyDeniedError", "message": str(exc)}})


_OUTCOMES: dict[
    ToolExecutionStatus,
    Literal["completed", "failed", "compensated", "compensation_failed"],
] = {
    Status.COMPLETED: "completed",
    Status.FAILED: "failed",
    Status.COMPENSATED: "compensated",
    Status.COMPENSATION_FAILED: "compensation_failed",
}
_ERRORS = {
    Status.FAILED: "ResolvedFailed",
    Status.COMPENSATION_FAILED: "CompensationFailed",
}


def _reason(reason: str, *, required: bool = False) -> str:
    cleaned = (reason or "").strip()
    if required and not cleaned:
        raise ValueError("a reason is required")
    if len(cleaned) > 1000:
        raise ValueError("reason must be at most 1000 characters")
    return cleaned
