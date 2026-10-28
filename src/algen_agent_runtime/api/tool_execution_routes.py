"""HTTP routes for reconciling recorded tool calls (``runtime/reconciliation.py``).

``GET /v1/tool-executions`` lists the caller's tenant's calls by status (``indeterminate`` and
``started`` by default), run, tool and minimum age; ``GET /v1/tool-executions/{key}`` reads one.
``POST .../resolve`` records an operator's finding, ``POST .../reconcile`` asks the tool's
``reconcile`` hook, ``POST .../compensate`` runs the tool's compensation and ``POST .../resume``
continues a run still paused on a settled call. Every route needs the ``tools:reconcile`` scope
and acts only within the caller's tenant; the caller's user id is recorded as the actor.
Responses never carry a tool result's value, only whether one is recorded.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.api.dependencies import Principal, require_scope
from algen_agent_runtime.runtime.reconciliation import (
    DEFAULT_RECONCILIATION_STATUSES,
    ReconciliationReport,
)
from algen_agent_runtime.tools.contracts import ToolExecutionRecord, ToolExecutionStatus

if TYPE_CHECKING:
    from algen_agent_runtime.orchestration.container import Container

RECONCILE_SCOPE = "tools:reconcile"


class ResolveToolExecutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "failed", "compensated", "compensation_failed"]
    reason: str = Field(min_length=1, max_length=1000)
    result: Any = None
    result_reference: str | None = Field(default=None, max_length=512)
    expected_status: ToolExecutionStatus | None = None
    resume: bool = True


class ReconcileToolExecutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="", max_length=1000)
    resume: bool = True


class CompensateToolExecutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=1000)
    expected_status: ToolExecutionStatus | None = None
    resume: bool = True


def tool_execution_view(record: ToolExecutionRecord) -> dict[str, Any]:
    """A record without its result value: what an operator needs to decide, nothing more."""
    view = record.model_dump(mode="json", exclude={"result"})
    view["has_result"] = record.result is not None and record.result.value is not None
    view["result_reference"] = (
        record.result.metadata.get("result_reference") if record.result is not None else None
    )
    return view


def _report(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "outcome": report.outcome,
        "run_resumed": report.run_resumed,
        "execution": tool_execution_view(report.record),
    }


def add_tool_execution_routes(app: FastAPI, container: Container, identity: Any) -> None:
    def reconciliation() -> Any:
        return container.runtime.reconciliation

    @app.get("/v1/tool-executions")
    async def list_tool_executions(
        status: Annotated[list[ToolExecutionStatus] | None, Query()] = None,
        run_id: str | None = None,
        tool: str | None = None,
        min_age_seconds: float | None = Query(default=None, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
        principal: Principal = identity,
    ) -> dict[str, Any]:
        require_scope(principal, RECONCILE_SCOPE)
        records = await reconciliation().list(
            principal.tenant_id,
            statuses=frozenset(status or DEFAULT_RECONCILIATION_STATUSES),
            run_id=run_id,
            tool_name=tool,
            min_age=timedelta(seconds=min_age_seconds) if min_age_seconds is not None else None,
            limit=limit,
        )
        return {"executions": [tool_execution_view(record) for record in records]}

    @app.get("/v1/tool-executions/{idempotency_key}")
    async def read_tool_execution(
        idempotency_key: str, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, RECONCILE_SCOPE)
        record = await reconciliation().get(principal.tenant_id, idempotency_key)
        return tool_execution_view(record)

    @app.post("/v1/tool-executions/{idempotency_key}/resolve")
    async def resolve_tool_execution(
        idempotency_key: str, body: ResolveToolExecutionBody, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, RECONCILE_SCOPE)
        try:
            report = await reconciliation().resolve(
                principal.tenant_id,
                idempotency_key,
                status=body.status,
                actor_id=principal.user_id,
                reason=body.reason,
                result=body.result,
                result_reference=body.result_reference,
                expected_status=body.expected_status,
                resume=body.resume,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report(report)

    @app.post("/v1/tool-executions/{idempotency_key}/reconcile")
    async def reconcile_tool_execution(
        idempotency_key: str, body: ReconcileToolExecutionBody, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, RECONCILE_SCOPE)
        try:
            report = await reconciliation().auto_reconcile(
                principal.tenant_id,
                idempotency_key,
                actor_id=principal.user_id,
                reason=body.reason,
                resume=body.resume,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report(report)

    @app.post("/v1/tool-executions/{idempotency_key}/compensate")
    async def compensate_tool_execution(
        idempotency_key: str, body: CompensateToolExecutionBody, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, RECONCILE_SCOPE)
        try:
            report = await reconciliation().compensate(
                principal.tenant_id,
                idempotency_key,
                actor_id=principal.user_id,
                reason=body.reason,
                expected_status=body.expected_status,
                resume=body.resume,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report(report)

    @app.post("/v1/tool-executions/{idempotency_key}/resume")
    async def resume_after_tool_execution(
        idempotency_key: str, principal: Principal = identity
    ) -> dict[str, Any]:
        require_scope(principal, RECONCILE_SCOPE)
        report = await reconciliation().resume(
            principal.tenant_id, idempotency_key, actor_id=principal.user_id
        )
        return _report(report)
