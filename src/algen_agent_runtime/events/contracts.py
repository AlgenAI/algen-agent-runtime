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
    "model.started",
    "model.delta",
    "model.completed",
    "tool.started",
    "tool.completed",
    "verification.completed",
    "clarification.required",
    "approval.required",
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
