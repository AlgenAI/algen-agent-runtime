from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PolicyAction(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REDACT = "redact"
    TRANSFORM = "transform"
    REQUIRE_CLARIFICATION = "require_clarification"
    REQUIRE_APPROVAL = "require_approval"


class PolicyPoint(StrEnum):
    """Stable trust boundaries at which runtime policies may be evaluated."""

    INPUT = "input"
    BEFORE_RETRIEVAL = "before_retrieval"
    AFTER_RETRIEVAL = "after_retrieval"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    PLAN_TOOL = "plan_tool"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    FINAL_RESPONSE = "final_response"
    BEFORE_MEMORY_WRITE = "before_memory_write"
    AFTER_MEMORY_WRITE = "after_memory_write"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: PolicyAction
    reason_code: str
    reason: str
    value: Any = None
    audit_metadata: dict[str, Any] = Field(default_factory=dict)
