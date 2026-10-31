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
    # The run's attachments, as a list of {artifact_id, media_type, name, size_bytes, sha256}
    # (never their content), before any model call: deny to refuse the run, transform to keep
    # a subset. Only policies that name this point see it.
    ATTACHMENTS = "attachments"


class PolicyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: PolicyAction
    reason_code: str
    reason: str
    value: Any = None
    audit_metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyFailMode(StrEnum):
    """What a boundary does when a policy raises or times out instead of deciding.

    ``closed`` refuses the value at that boundary, as a ``deny`` decision would. ``open`` skips
    the failed policy and lets the remaining policies decide; it never skips the others.
    """

    CLOSED = "closed"
    OPEN = "open"


#: Reason code of the ``deny`` decision a boundary returns when a policy failed closed.
POLICY_ERROR_REASON_CODE = "policy.error"


class PolicyErrorHandling(BaseModel):
    """Fail mode per boundary, and how long one policy may take to decide."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    on_error: PolicyFailMode = PolicyFailMode.CLOSED
    boundary_on_error: dict[PolicyPoint, PolicyFailMode] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0)

    def mode(self, point: PolicyPoint | str) -> PolicyFailMode:
        return self.boundary_on_error.get(PolicyPoint(point), self.on_error)
