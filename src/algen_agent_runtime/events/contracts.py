from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.types.contracts import utc_now

EventType = Literal[
    "run.started",
    "step.started",
    "context.retrieved",
    "context.compaction.assessed",
    "context.compacted",
    "context.working_state.updated",
    "context.skills.updated",
    "model.started",
    "model.delta",
    "model.completed",
    "model.retrying",
    "budget.refused",
    "tool.started",
    "tool.completed",
    "tool.refused",
    "tool.repeated",
    "tool.wrap_up",
    "tool.reconciliation_required",
    "tool.reconciled",
    "tool.compensation.started",
    "tool.compensation.completed",
    "tool.compensation.failed",
    "verification.completed",
    "policy.evaluated",
    "memory.written",
    "memory.learned.updated",
    "clarification.required",
    "approval.required",
    "mcp.auth_required",
    "run.continued",
    "run.completed",
    "run.failed",
    "run.cancelled",
]


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: EventType
    run_id: str
    tenant_id: str
    session_id: str
    correlation_id: str
    conversation_id: str | None = None
    turn_id: str | None = None
    parent_run_id: str | None = None
    workflow_run_id: str | None = None
    step_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(default_factory=lambda: str(uuid4()))
    action: str
    outcome: str
    tenant_id: str
    actor_id: str
    resource_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
