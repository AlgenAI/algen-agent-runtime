from __future__ import annotations

import hashlib
import json
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
    context_window_tokens: int | None = Field(default=None, ge=1)
    context_safety_margin_tokens: int = Field(default=256, ge=0)
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


class LearnedMemoryPolicy(StrictModel):
    enabled: bool = False
    max_recall_tokens: int = Field(default=2048, ge=128, le=8192)
    max_retention_seconds: int = Field(default=2592000, ge=60, le=31536000)


class ContextPolicy(StrictModel):
    skills_enabled: bool = False
    skill_catalog_max_tokens: int = Field(default=2048, ge=128, le=8192)
    loaded_skills_max_tokens: int = Field(default=4096, ge=256, le=16384)
    working_state_enabled: bool = False
    working_state_max_tokens: int = Field(default=2048, ge=128, le=8192)
    offload_tool_results: bool = False
    tool_result_max_inline_bytes: int = Field(default=16_384, ge=1024)
    tool_result_preview_chars: int = Field(default=256, ge=0, le=1024)
    artifact_retention_seconds: int = Field(default=86_400, ge=1)
    compact_history: bool = False
    archive_compacted_history: bool = False
    compaction_trigger_tokens: int = Field(default=12_000, ge=256)
    compaction_trigger_ratio: float | None = Field(default=0.8, gt=0, le=1)
    compaction_keep_recent_groups: int = Field(default=6, ge=1)
    compaction_max_input_tokens: int = Field(default=8_000, ge=256)
    compaction_max_output_tokens: int = Field(default=1_024, ge=64)

    @model_validator(mode="after")
    def archive_requires_compaction(self) -> ContextPolicy:
        if self.archive_compacted_history and not self.compact_history:
            raise ValueError("archive_compacted_history requires compact_history")
        return self


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


SkillName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]


class SkillDefinition(StrictModel):
    name: SkillName
    version: str = Field(pattern=r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
    description: str = Field(min_length=1, max_length=512)
    instructions: str = Field(min_length=1, max_length=16384)
    required_permissions: frozenset[str] = frozenset()

    @property
    def sha256(self) -> str:
        # Sort the permission set for a stable identity across worker processes.
        value = self.model_dump(mode="json")
        value["required_permissions"] = sorted(self.required_permissions)
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SkillSelection(StrictModel):
    names: tuple[SkillName, ...] = Field(max_length=4)

    @model_validator(mode="after")
    def unique_names(self) -> SkillSelection:
        if len(set(self.names)) != len(self.names):
            raise ValueError("skill selection names must be unique")
        return self


class LoadedSkill(StrictModel):
    name: SkillName
    version: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    instructions: str = Field(min_length=1, max_length=16384)


class LoadedSkills(StrictModel):
    skills: tuple[LoadedSkill, ...] = Field(max_length=4)

    @model_validator(mode="after")
    def unique_names(self) -> LoadedSkills:
        if len({skill.name for skill in self.skills}) != len(self.skills):
            raise ValueError("loaded skill names must be unique")
        return self


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
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    skills: tuple[SkillDefinition, ...] = Field(default=(), max_length=32)
    planning_strategy: str = "react"
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    learned_memory_policy: LearnedMemoryPolicy = Field(default_factory=LearnedMemoryPolicy)
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

    @model_validator(mode="after")
    def unique_skills(self) -> AgentDefinition:
        if len({skill.name for skill in self.skills}) != len(self.skills):
            raise ValueError("agent skill names must be unique")
        return self

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


class ArtifactStatus(StrEnum):
    """Lifecycle state for an uploaded or Runtime-produced artifact."""

    PENDING_SCAN = "pending_scan"
    AVAILABLE = "available"
    QUARANTINED = "quarantined"


class ArtifactDescriptor(StrictModel):
    """Artifact metadata safe to return without loading the blob payload."""

    id: str
    tenant_id: str
    run_id: str | None = None
    name: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ArtifactStatus = ArtifactStatus.AVAILABLE
    metadata: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Artifact(StrictModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    run_id: str | None = None
    name: str
    media_type: str
    data: bytes
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: ArtifactStatus = ArtifactStatus.AVAILABLE
    metadata: dict[str, str] = Field(default_factory=dict)
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def verify_integrity(self) -> Artifact:
        size = len(self.data)
        checksum = hashlib.sha256(self.data).hexdigest()
        if self.size_bytes is not None and self.size_bytes != size:
            raise ValueError("artifact size_bytes does not match data")
        if self.sha256 is not None and self.sha256 != checksum:
            raise ValueError("artifact sha256 does not match data")
        return self

    def descriptor(self) -> ArtifactDescriptor:
        return ArtifactDescriptor(
            **self.model_dump(exclude={"data", "size_bytes", "sha256"}),
            size_bytes=len(self.data),
            sha256=hashlib.sha256(self.data).hexdigest(),
        )


class Citation(StrictModel):
    id: str
    document_id: str
    chunk_id: str
    source: str
    title: str | None = None
    uri: str | None = None
    score: float = Field(ge=0, le=1)


class ContextCompaction(StrictModel):
    through: int = Field(ge=1)
    summary: str
    archive_id: str


WorkingStateItem = Annotated[str, Field(min_length=1, max_length=512)]
WorkingStateItems = Annotated[tuple[WorkingStateItem, ...], Field(max_length=16)]


class WorkingStateContent(StrictModel):
    goal: str = Field(max_length=2048)
    constraints: WorkingStateItems
    decisions: WorkingStateItems
    completed_work: WorkingStateItems
    open_tasks: WorkingStateItems


class WorkingState(StrictModel):
    revision: int = Field(default=0, ge=0, strict=True)
    state: WorkingStateContent = Field(
        default_factory=lambda: WorkingStateContent(
            goal="", constraints=(), decisions=(), completed_work=(), open_tasks=()
        )
    )


class WorkingStateUpdate(StrictModel):
    expected_revision: int = Field(ge=0, strict=True)
    state: WorkingStateContent


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
    context_artifact_ids: list[str] = Field(default_factory=list)
    context_history: list[Message] | None = None
    context_history_offset: int = Field(default=0, ge=0)
    context_history_pinned: list[Message] = Field(default_factory=list)
    context_compaction: ContextCompaction | None = None
    working_state: WorkingState | None = None
    loaded_skills: LoadedSkills = Field(default_factory=lambda: LoadedSkills(skills=()))
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)
    completed_tool_call_ids: set[str] = Field(default_factory=set)
    approved_tool_call_ids: set[str] = Field(default_factory=set)
    output_text: str | None = None
    error: str | None = None
    pause_payload: dict[str, Any] | None = None
    citations: list[Citation] = Field(default_factory=list)
    summary: ExecutionSummary = Field(default_factory=ExecutionSummary)
    version: int = 0

    @model_validator(mode="after")
    def archived_history_is_consistent(self) -> RunState:
        if self.context_history_offset:
            if self.context_history is None or self.context_compaction is None:
                raise ValueError("archived history requires a compaction cursor and retained tail")
            if (
                not self.context_history_offset
                <= self.context_compaction.through
                <= (self.context_history_offset + len(self.context_history))
            ):
                raise ValueError("compaction cursor is outside retained history")
        elif self.context_history_pinned:
            raise ValueError("pinned history requires an archived prefix")
        if any(
            message.role not in {Role.SYSTEM, Role.USER} for message in self.context_history_pinned
        ):
            raise ValueError("pinned history can contain only system and user messages")
        if sum(message.role == Role.USER for message in self.context_history_pinned) > 1:
            raise ValueError("pinned history can contain at most one user message")
        return self

    def append_message(self, message: Message) -> None:
        self.messages.append(message)
        if self.context_history is not None:
            self.context_history.append(message)


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
