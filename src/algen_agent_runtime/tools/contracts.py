from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.types.contracts import RetryPolicy


class SideEffect(StrEnum):
    NONE = "none"
    READ = "read"
    WRITE = "write"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"


class Idempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    KEYED = "keyed"
    NON_IDEMPOTENT = "non_idempotent"


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    version: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_permissions: frozenset[str] = frozenset()
    side_effect: SideEffect = SideEffect.NONE
    idempotency: Idempotency = Idempotency.IDEMPOTENT
    timeout_seconds: float = Field(default=30, gt=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    cost_usd: float = Field(default=0, ge=0)
    max_concurrency: int = Field(default=10, ge=1)
    max_result_bytes: int = Field(default=1_048_576, ge=1)
    audit_metadata: dict[str, str] = Field(default_factory=dict)


class ToolContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)
    run_id: str
    step_id: str
    tenant_id: str
    user_id: str
    permissions: frozenset[str]
    idempotency_key: str
    secrets: Mapping[str, str] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    redacted: bool = False


class ToolExecutionStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"


class ToolExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    idempotency_key: str
    run_id: str
    step_id: str
    tenant_id: str
    tool_name: str
    status: ToolExecutionStatus
    result: ToolResult | None = None
    error: str | None = None


class ToolExecutionStore(Protocol):
    async def begin(
        self, context: ToolContext, tool_name: str
    ) -> tuple[ToolExecutionRecord, bool]: ...

    async def complete(self, idempotency_key: str, result: ToolResult) -> None: ...
    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None: ...


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[Any] | Any]
CompensationHandler = Callable[[ToolResult, ToolContext], Awaitable[None] | None]


class Tool:
    def __init__(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
        compensation: CompensationHandler | None = None,
    ) -> None:
        self.definition = definition
        self.handler = handler
        self.compensation = compensation
