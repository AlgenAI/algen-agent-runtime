from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class TextBlock(StrictModel):
    type: Literal["text"] = "text"
    text: str


class ImageBlock(StrictModel):
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    media_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> ImageBlock:
        if bool(self.url) == bool(self.data):
            raise ValueError("exactly one of url or data is required")
        return self


class FileBlock(StrictModel):
    type: Literal["file"] = "file"
    artifact_id: str
    media_type: str | None = None


ContentBlock = Annotated[TextBlock | ImageBlock | FileBlock, Field(discriminator="type")]


class Message(StrictModel):
    role: Role
    content: tuple[ContentBlock, ...]
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @classmethod
    def text(cls, role: Role, text: str) -> Message:
        return cls(role=role, content=(TextBlock(text=text),))

    @property
    def text_content(self) -> str:
        return "\n".join(block.text for block in self.content if isinstance(block, TextBlock))


class ToolCall(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(StrictModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    estimated_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    CANCELLED = "cancelled"


class ErrorKind(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ModelCapabilities(StrictModel):
    chat: bool = True
    streaming: bool = False
    tools: bool = False
    structured_output: bool = False
    json_schema: bool = False
    embeddings: bool = False
    images: bool = False
    files: bool = False


class ToolSpec(StrictModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ModelRequest(StrictModel):
    messages: tuple[Message, ...]
    model: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    response_schema: dict[str, Any] | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    timeout_seconds: float | None = None
    stream: bool = False
    raw_response_enabled: bool = False
    extensions: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    message: Message
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0
    model: str
    provider: str
    raw_metadata: dict[str, Any] | None = None


class ModelStreamEvent(StrictModel):
    type: Literal["delta", "tool_call", "completed", "error"]
    delta: str | None = None
    tool_call: ToolCall | None = None
    response: ModelResponse | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None


class ModelProfile(StrictModel):
    name: str
    provider: str | None = None
    model: str | None = None
    required_capabilities: frozenset[str] = frozenset({"chat"})
    quality_tier: int = Field(default=1, ge=0, le=5)
    max_cost_per_1k_tokens: float | None = Field(default=None, ge=0)
    max_latency_ms: float | None = Field(default=None, ge=0)
    routing_preference: Literal["balanced", "local_first", "cloud_first"] = "balanced"
    extensions: dict[str, Any] = Field(default_factory=dict)


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=3, ge=1, le=20)
    initial_backoff_seconds: float = Field(default=0.1, ge=0)
    max_backoff_seconds: float = Field(default=5.0, ge=0)
    retryable_errors: frozenset[ErrorKind] = frozenset(
        {ErrorKind.RATE_LIMIT, ErrorKind.TIMEOUT, ErrorKind.UNAVAILABLE}
    )


class Budget(StrictModel):
    max_tokens: int = Field(default=100_000, ge=1)
    max_output_tokens: int = Field(default=8_192, ge=1)
    max_cost_usd: float = Field(default=10.0, ge=0)
    max_latency_seconds: float = Field(default=300.0, gt=0)


class MemoryPolicy(StrictModel):
    enabled: bool = True
    retention_seconds: int = Field(default=86_400, ge=0)
    max_items: int = Field(default=200, ge=0)
    store_tool_payloads: bool = False
    redact_pii: bool = True


class GuardrailPolicy(StrictModel):
    policies: tuple[str, ...] = (
        "secrets",
        "pii",
        "prompt_injection",
        "output_validation",
        "authorization",
    )
    require_citations: bool = False


class VerificationPolicy(StrictModel):
    verifiers: tuple[str, ...] = ("non_empty",)
    minimum_confidence: float = Field(default=0.0, ge=0, le=1)
    max_repairs: int = Field(default=1, ge=0, le=10)


class ApprovalPolicy(StrictModel):
    require_for_side_effects: bool = True
    require_for_destructive: bool = True
    expires_seconds: int = Field(default=3600, ge=1)


class AgentDefinition(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    description: str
    capabilities: frozenset[str] = frozenset()
    system_instructions: str
    prompt_templates: dict[str, str] = Field(default_factory=dict)
    default_model: ModelProfile
    fallback_models: tuple[ModelProfile, ...] = ()
    enabled_tools: frozenset[str] = frozenset()
    tool_permissions: frozenset[str] = frozenset()
    context_builder: str = "default"
    planning_strategy: str = "react"
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    guardrail_policy: GuardrailPolicy = Field(default_factory=GuardrailPolicy)
    verification_policy: VerificationPolicy = Field(default_factory=VerificationPolicy)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    response_composer: str = "default"
    budget: Budget = Field(default_factory=Budget)
    max_steps: int = Field(default=16, ge=1, le=1000)
    workflow: dict[str, Any] | None = None
    domain_plugins: tuple[str, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    model_allowlist: frozenset[str] = frozenset()

    @property
    def logical_id(self) -> str:
        """Stable telemetry and policy identity, independent of definition version."""
        return self.name

    @property
    def key(self) -> str:
        """Versioned definition key used for loading and reproducibility."""
        return f"{self.name}@{self.version}"


class RunStatus(StrEnum):
    RECEIVED = "received"
    VALIDATING = "validating"
    BUILDING_CONTEXT = "building_context"
    PLANNING = "planning"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    AWAITING_APPROVAL = "awaiting_approval"
    INVOKING_MODEL = "invoking_model"
    INVOKING_TOOL = "invoking_tool"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    COMPOSING = "composing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
)
PAUSED_STATUSES = frozenset({RunStatus.AWAITING_CLARIFICATION, RunStatus.AWAITING_APPROVAL})


class RunRequest(StrictModel):
    agent: str
    agent_version: str | None = None
    input: str
    session_id: str | None = None
    tenant_id: str
    user_id: str
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str | None = None
    turn_id: str | None = None
    parent_run_id: str | None = None
    workflow_run_id: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0)
    stream: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)
    overrides: RequestOverrides = Field(default_factory=lambda: RequestOverrides())


class RequestOverrides(StrictModel):
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    response_schema: dict[str, Any] | None = None


class ExecutionSummary(StrictModel):
    decisions: tuple[str, ...] = ()
    model_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    usage: TokenUsage = Field(default_factory=TokenUsage)


class Artifact(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    run_id: str
    name: str
    media_type: str
    data: bytes
    created_at: datetime = Field(default_factory=utc_now)


class Citation(StrictModel):
    id: str
    document_id: str
    chunk_id: str
    source: str
    title: str | None = None
    uri: str | None = None
    score: float = Field(ge=0, le=1)


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    request: RunRequest
    agent_key: str | None = None
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    status: RunStatus = RunStatus.RECEIVED
    step_count: int = 0
    attempt_count: int = 0
    started_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deadline: datetime | None = None
    messages: list[Message] = Field(default_factory=list)
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)
    completed_tool_call_ids: set[str] = Field(default_factory=set)
    approved_tool_call_ids: set[str] = Field(default_factory=set)
    output_text: str | None = None
    error: str | None = None
    pause_payload: dict[str, Any] | None = None
    citations: list[Citation] = Field(default_factory=list)
    summary: ExecutionSummary = Field(default_factory=ExecutionSummary)
    version: int = 0


class RunResult(StrictModel):
    run_id: str
    status: RunStatus
    session_id: str
    output: str | None = None
    error: str | None = None
    artifacts: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    warnings: tuple[str, ...] = ()
    confidence: float | None = None
    execution_summary: ExecutionSummary
