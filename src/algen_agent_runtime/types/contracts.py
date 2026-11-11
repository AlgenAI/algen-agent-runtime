from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)


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
    """A file by reference. History, memory and checkpoints hold only the reference; the
    Runtime loads the bytes onto a copy of the block just before a model call
    (:meth:`with_content`), and that copy is never stored or serialized."""

    type: Literal["file"] = "file"
    artifact_id: str
    media_type: str | None = None
    _content: bytes | None = PrivateAttr(default=None)
    _name: str | None = PrivateAttr(default=None)

    def with_content(self, content: bytes, *, name: str, media_type: str) -> FileBlock:
        resolved = self.model_copy(update={"media_type": media_type})
        resolved._content = content
        resolved._name = name
        return resolved

    @property
    def content(self) -> bytes | None:
        """The file's bytes, on a block resolved for a model call; None otherwise."""
        return self._content

    @property
    def name(self) -> str | None:
        return self._name


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
    # ``input_tokens`` is the whole prompt; ``cached_tokens`` the part read from the provider's
    # prompt cache and ``cache_write_tokens`` the part written to it.
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
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
    routing_preference: Literal["balanced", "local_first", "cloud_first", "ordered"] = "balanced"
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
    max_model_calls: int = Field(
        default=1_000,
        ge=1,
        description="Model calls one run may make (steps, retried rejections, compaction and "
        "repairs). A continuation adds continuation_policy.step_increment.",
    )
    max_response_chars: int = Field(
        default=1_000_000,
        ge=1_000,
        description="Characters one model response may carry: its text plus its tool calls' "
        "arguments as JSON. A longer response fails the run, streamed or not.",
    )
    max_events: int = Field(
        default=100_000,
        ge=100,
        description="Run events one run may record. Past it, only events that end or pause the "
        "run are recorded, and the run fails at its next step.",
    )


class MemoryPolicy(StrictModel):
    enabled: bool = True
    retention_seconds: int = Field(default=86_400, ge=0)
    max_items: int = Field(default=200, ge=0)
    store_tool_payloads: bool = False
    tool_payload_max_chars: int = Field(default=4_000, ge=256, le=200_000)
    redact_pii: bool = True


class LearnedMemoryPolicy(StrictModel):
    enabled: bool = False
    max_recall_tokens: int = Field(default=2048, ge=128, le=8192)
    max_retention_seconds: int = Field(default=2592000, ge=60, le=31536000)
    # How core.memory_recall ranks a non-empty query: ``substring`` matches keys and content
    # literally; ``semantic`` ranks by embedding similarity; ``hybrid`` combines both.
    recall: Literal["substring", "semantic", "hybrid"] = "substring"
    # Semantic and hybrid recall drop records scoring below this (0-1; cosine similarity for
    # ``semantic``, the weighted score for ``hybrid``).
    min_score: float = Field(default=0.0, ge=0, le=1)
    # The weight of a key or substring match in a ``hybrid`` score; similarity gets the rest.
    hybrid_keyword_weight: float = Field(default=0.3, ge=0, le=1)
    # A registered model provider (its registration id) and model that embed record content and
    # recall queries. ``embedding_version`` names the vectors' generation: change it to re-embed
    # when the provider serves a different model under the same name.
    embedding_provider: str | None = Field(default=None, min_length=1, max_length=128)
    embedding_model: str | None = Field(default=None, min_length=1, max_length=256)
    embedding_version: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def similarity_needs_embeddings(self) -> LearnedMemoryPolicy:
        if self.recall != "substring" and self.embedding_provider is None:
            raise ValueError(
                f"learned_memory_policy.recall {self.recall!r} needs embedding_provider"
            )
        return self

    @property
    def embedding_identity(self) -> str | None:
        """The identity recorded with each vector: vectors of another identity are not compared."""
        if self.recall == "substring" or self.embedding_provider is None:
            return None
        return json.dumps(
            [self.embedding_provider, self.embedding_model, self.embedding_version],
            separators=(",", ":"),
        )


DEFAULT_SHORTENED_TOOL_RESULT_NOTE = (
    "\n[earlier tool result shortened to fit the context; it was already retrieved in this run]"
)


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
    # Older tool results that no longer fit are cut to their opening instead of being dropped, so a
    # long run still knows what it already retrieved and does not look it up again.
    shorten_older_tool_results: bool = False
    shortened_tool_result_chars: int = Field(default=1_200, ge=64, le=16_384)
    # Share of the free budget held back for the shortened older results; whole results fill the
    # rest, newest first. Used only when the history does not fit whole.
    shortened_tool_results_share: float = Field(default=0.25, gt=0, lt=1)
    # Appended to each shortened result so the model knows it already has it. Empty: no note.
    shortened_tool_result_note: str = Field(
        default=DEFAULT_SHORTENED_TOOL_RESULT_NOTE, max_length=512
    )

    @model_validator(mode="after")
    def archive_requires_compaction(self) -> ContextPolicy:
        if self.archive_compacted_history and not self.compact_history:
            raise ValueError("archive_compacted_history requires compact_history")
        return self


PIIEntity = Literal["email", "phone", "aadhaar", "card"]


class PIIGuardrail(StrictModel):
    """Settings for the built-in ``pii`` policy.

    ``exempt_entities`` lists the kinds of identifier this agent does not redact anywhere, for
    an agent whose job is to handle them (a support desk that reads customers' phone numbers,
    say). To exempt one tool field rather than a whole kind, annotate the field in the tool's
    schema (``"format": "identifier"`` or ``"x-pii": false``).
    """

    exempt_entities: frozenset[PIIEntity] = frozenset()


class GuardrailPolicy(StrictModel):
    policies: tuple[str, ...] = (
        "secrets",
        "pii",
        "prompt_injection",
        "output_validation",
        "authorization",
    )
    require_citations: bool = False
    # Policies applied to tool arguments and results. ``None`` applies ``policies``; an
    # agent that edits code can keep secret redaction on its answers but not on its files.
    tool_policies: tuple[str, ...] | None = None
    # Tool results that look like instructions to the model: ``flag`` marks the result as
    # untrusted data before the model reads it, ``block`` fails the tool call.
    tool_result_injection: Literal["off", "flag", "block"] = "off"
    pii: PIIGuardrail = Field(default_factory=PIIGuardrail)

    @model_serializer(mode="wrap")
    def _omit_default_pii(self, handler: SerializerFunctionWrapHandler) -> Any:
        # An agent that does not set pii serialises, and so fingerprints, as it did in 0.2.0b1,
        # and a b1 replica (extra="forbid") still loads it.
        data = handler(self)
        if isinstance(data, dict) and not self.pii.exempt_entities:
            data.pop("pii", None)
        return data


class VerificationPolicy(StrictModel):
    verifiers: tuple[str, ...] = ("non_empty",)
    minimum_confidence: float = Field(default=0.0, ge=0, le=1)
    max_repairs: int = Field(default=1, ge=0, le=10)


class ApprovalPolicy(StrictModel):
    require_for_side_effects: bool = True
    require_for_destructive: bool = True
    expires_seconds: int = Field(default=3600, ge=1)
    # ``continue`` tells the model the action was declined and lets the run go on.
    on_rejection: Literal["fail_run", "continue"] = "fail_run"


class ToolPolicy(StrictModel):
    # ``return_to_model`` reports a failed tool call to the model as the call's result,
    # so it can correct itself, instead of failing the run.
    on_error: Literal["fail_run", "return_to_model"] = "fail_run"
    # Run the tool calls one model reply requests concurrently when none needs approval.
    parallel_calls: bool = False
    max_parallel_calls: int = Field(default=4, ge=1, le=16)
    max_refusals_per_tool: int | None = Field(
        default=None,
        ge=1,
        description="Refused calls to one tool (a failed precheck or arguments that do not fit "
        "its input schema, counted as ``refusal_count`` says) after which the refusal tells the model to stop calling it and "
        "tell the user what was refused and why. None always says to fix the call and try again.",
    )
    refusal_count: Literal["consecutive", "per_run"] = Field(
        default="consecutive",
        description="How refusals count toward ``max_refusals_per_tool``. ``consecutive`` starts "
        "a tool's count again when a call to it runs successfully, so a model that is making "
        "progress is not stopped; ``per_run`` counts every refusal in the run.",
    )
    repeat_guard: bool = Field(
        default=False,
        description="A read-only call identical to an earlier one in the run, with no successful "
        "state-changing call since, returns a note instead of running. Tools marked volatile are "
        "never held back.",
    )
    max_calls_per_tool: dict[str, Annotated[int, Field(ge=1)]] = Field(
        default_factory=dict,
        description="The most times each named tool may run in one run; past it, a call returns "
        "a note to answer from what the tool already returned.",
    )
    on_indeterminate: Literal["on_error", "pause"] = Field(
        default="on_error",
        description="What a side-effecting call whose outcome is unknown (recorded "
        "indeterminate, or found still started when replayed) does to the run. ``on_error`` "
        "treats it like any failed call, per ``on_error``. ``pause`` pauses the run in "
        "``awaiting_reconciliation`` until an operator resolves the call; the run then continues "
        "with the recorded result, or with a tool error when it did not take effect.",
    )


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


class ContinuationPolicy(StrictModel):
    """Progressive execution budgets with an approval-backed escape hatch.

    The base agent budget remains a real safety boundary. A configured number of
    small extensions may be granted automatically; every later extension pauses
    durably for a human decision instead of failing the run.
    """

    enabled: bool = False
    automatic_extensions: int = Field(default=0, ge=0, le=10)
    step_increment: int = Field(default=16, ge=1, le=1000)
    token_increment: int = Field(default=8_000, ge=1)
    cost_increment_usd: float = Field(default=0.25, ge=0)
    approval_expires_seconds: int = Field(default=3600, ge=1)


class WrapUpPolicy(StrictModel):
    """Room to answer: tell the model to stop calling tools while it can still answer.

    A run that uses its last step or token on a tool call ends without an answer. When enabled,
    every tool result in a run that can afford at most ``tool_calls_left`` more tool calls carries
    a notice to wrap up and answer. The token budget shortens that: from ``token_wrap_up_share``
    of it the run gets at most ``tool_calls_left`` more calls, from ``token_answer_share`` none.
    """

    enabled: bool = Field(default=False, description="Append the wrap-up notice to tool results.")
    tool_calls_left: int = Field(
        default=2,
        ge=0,
        le=100,
        description="Warn once the run can afford this many more tool calls (each with the model "
        "call that reads its result) or fewer before it must answer.",
    )
    token_wrap_up_share: float = Field(
        default=0.75,
        gt=0,
        le=1,
        description="Share of the token budget from which the run wraps up.",
    )
    token_answer_share: float = Field(
        default=0.9,
        gt=0,
        le=1,
        description="Share of the token budget from which the model must answer at once.",
    )

    @model_validator(mode="after")
    def shares_in_order(self) -> WrapUpPolicy:
        if self.token_wrap_up_share > self.token_answer_share:
            raise ValueError("token_wrap_up_share cannot exceed token_answer_share")
        return self


DEFAULT_ATTACHMENT_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
)
# The types a model reads natively (vision or PDF input): what ``AttachmentPolicy.types`` lists.
MODEL_ATTACHMENT_TYPES = DEFAULT_ATTACHMENT_TYPES
# Text types. The bytes only say "text", so the declared type tells them apart.
TEXT_ATTACHMENT_TYPES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/tab-separated-values",
        "text/markdown",
        "text/html",
        "text/xml",
        "application/xml",
        "application/json",
    }
)
# The types a run may carry for a tool to read (``AttachmentPolicy.tool_read_types``). They never
# reach a model, which sees ``[attachment: name, type, id=...]`` instead, and each is recognised
# from its bytes.
TOOL_READ_ATTACHMENT_TYPES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-outlook",
        "message/rfc822",
        "image/heic",
        *TEXT_ATTACHMENT_TYPES,
    }
)
# Every type a run may carry: the deployment's default ceiling (security.allowed_attachment_types).
# Agents still opt in to each type.
KNOWN_ATTACHMENT_TYPES = MODEL_ATTACHMENT_TYPES | TOOL_READ_ATTACHMENT_TYPES


class RunAttachment(StrictModel):
    """A file given to a run, by reference to an uploaded artifact; never inline bytes."""

    artifact_id: str = Field(min_length=1, max_length=128)
    media_type: str | None = Field(default=None, max_length=127)


class AttachmentPolicy(StrictModel):
    """Whether an agent (or workflow) accepts files, and which.

    Off by default. ``retention`` says how long a file stays in what the model reads: ``turn``
    replaces it with a short note once the turn ends, so it is not billed on every later turn;
    ``session`` keeps it for the session's later turns.

    ``types`` are read by the model; ``tool_read_types`` (Office documents, CSV, text, e-mail)
    never reach a model: it sees ``[attachment: name, type, id=...]`` and hands the id to a
    reader tool, which calls ``ToolContext.read_attachment``.
    """

    enabled: bool = False
    types: frozenset[str] = DEFAULT_ATTACHMENT_TYPES
    tool_read_types: frozenset[str] = Field(
        default=frozenset(),
        description="Types a run may carry for a tool to read; they never reach a model. A "
        "trailing '*' (application/vnd.openxmlformats-officedocument.*) matches every known "
        "type with that prefix. None by default.",
    )
    max_count: int = Field(default=5, ge=1, le=32)
    retention: Literal["turn", "session"] = "turn"

    @field_validator("tool_read_types", mode="before")
    @classmethod
    def _expand_patterns(cls, value: Any) -> Any:
        if isinstance(value, str) or not isinstance(value, list | tuple | set | frozenset):
            return value
        expanded: set[Any] = set()
        for item in value:
            if isinstance(item, str) and item.endswith("*"):
                matched = {
                    known for known in TOOL_READ_ATTACHMENT_TYPES if known.startswith(item[:-1])
                }
                if not matched:
                    raise ValueError(f"tool_read_types: {item!r} matches no type a tool can read")
                expanded |= matched
            else:
                expanded.add(item)
        return frozenset(expanded)

    @model_validator(mode="after")
    def _types_known_and_apart(self) -> AttachmentPolicy:
        both = self.types & self.tool_read_types
        if both:
            raise ValueError(
                f"{', '.join(sorted(both))} is listed in both types (read by the model) and "
                "tool_read_types (read by a tool); list each type once"
            )
        unreadable = self.types & TOOL_READ_ATTACHMENT_TYPES
        if unreadable:
            raise ValueError(
                f"types: a model cannot read {', '.join(sorted(unreadable))}; list it under "
                "tool_read_types for a tool to read"
            )
        unknown = self.tool_read_types - TOOL_READ_ATTACHMENT_TYPES
        if unknown:
            raise ValueError(
                f"tool_read_types: {', '.join(sorted(unknown))} cannot be recognised from a "
                f"file's bytes; known: {', '.join(sorted(TOOL_READ_ATTACHMENT_TYPES))}"
            )
        return self

    @model_serializer(mode="wrap")
    def _omit_empty_tool_read_types(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A manifest that does not use tool_read_types keeps its earlier fingerprint.
        data = handler(self)
        if isinstance(data, dict) and not self.tool_read_types:
            data.pop("tool_read_types", None)
        return data


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
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    continuation_policy: ContinuationPolicy = Field(default_factory=ContinuationPolicy)
    wrap_up_policy: WrapUpPolicy = Field(default_factory=WrapUpPolicy)
    response_composer: str = "default"
    budget: Budget = Field(default_factory=Budget)
    max_steps: int = Field(default=16, ge=1, le=1000)
    workflow: dict[str, Any] | None = None
    domain_plugins: tuple[str, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    model_allowlist: frozenset[str] = frozenset()
    attachments: AttachmentPolicy = Field(default_factory=AttachmentPolicy)
    delegates: frozenset[str] = Field(
        default=frozenset(),
        description="The agents this agent may delegate to with core.subagent, by name; '*' "
        "allows any agent of the registry. Empty (the default) allows none.",
    )

    @field_validator("delegates")
    @classmethod
    def _delegate_names(cls, value: frozenset[str]) -> frozenset[str]:
        invalid = sorted(
            name
            for name in value
            if name != "*" and not re.fullmatch(r"[a-z][a-z0-9_.-]{1,127}", name)
        )
        if invalid:
            raise ValueError(f"delegates must be agent names or '*': {invalid}")
        return value

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
    # Paused on a tool call whose outcome is unknown until an operator settles it (only with
    # ``ToolPolicy.on_indeterminate="pause"``).
    AWAITING_RECONCILIATION = "awaiting_reconciliation"
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
PAUSED_STATUSES = frozenset(
    {
        RunStatus.AWAITING_CLARIFICATION,
        RunStatus.AWAITING_APPROVAL,
        RunStatus.AWAITING_RECONCILIATION,
    }
)


class RunRequest(StrictModel):
    agent: str
    agent_version: str | None = None
    input: str
    attachments: tuple[RunAttachment, ...] = Field(default=(), max_length=32)
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

    @model_serializer(mode="wrap")
    def _omit_empty_attachments(self, handler: SerializerFunctionWrapHandler) -> Any:
        # 0.1.0a18 validates stored run state with extra="forbid" and has no attachments: leave
        # them out while empty, so a18 replicas can still load what a19 stores.
        data = handler(self)
        if isinstance(data, dict) and not self.attachments:
            data.pop("attachments", None)
        return data


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


def canonical_digest(value: Any) -> str:
    """SHA-256 (hex) of a value's canonical JSON: sorted keys, no whitespace.

    Approvals bind a call to this digest of its arguments, and audit events carry it in place of
    the arguments themselves.
    """
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_SHA256_HEX = r"^[0-9a-f]{64}$"


class ToolApproval(StrictModel):
    """A decided approval, bound to the one call it covers.

    ``ToolExecutor.execute`` takes it as ``approval=`` and refuses the call unless ``tool``,
    ``tool_version`` and ``arguments_sha256`` match the call it is asked to run, and, when the
    approval recorded one, ``tool_fingerprint`` matches the tool's current definition and code.
    The caller that builds it (an agent run, the workflow engine, or your own approval flow)
    proves the decision itself: see ``docs/tool-authoring.md``, "Who proves an approval is valid".
    """

    approval_id: str = Field(min_length=1)
    """The decided approval: an approval request id, or the workflow approval node."""
    decision: Literal["approved", "modified"]
    """Only an approving decision covers a call."""
    tool: str = Field(min_length=1)
    tool_version: str
    arguments_sha256: str = Field(pattern=_SHA256_HEX)
    """:func:`canonical_digest` of the arguments the approver approved (or modified to)."""
    tool_fingerprint: str | None = Field(default=None, pattern=_SHA256_HEX)
    """The tool's fingerprint when the approval was asked for; None on approvals recorded
    before fingerprints, which are not checked against the tool's definition."""

    @classmethod
    def for_call(
        cls,
        approval_id: str,
        decision: Literal["approved", "modified"],
        tool: str,
        tool_version: str,
        arguments: Any,
        *,
        tool_fingerprint: str | None = None,
    ) -> ToolApproval:
        """The approval of a call with exactly these ``arguments``."""
        return cls(
            approval_id=approval_id,
            decision=decision,
            tool=tool,
            tool_version=tool_version,
            arguments_sha256=canonical_digest(arguments),
            tool_fingerprint=tool_fingerprint,
        )


# Bounds on steering (``AgentRuntime.steer``): messages queued for one run, and characters each.
MAX_PENDING_STEERING = 16
MAX_STEERING_CHARS = 8_000


class SteeringMessage(StrictModel):
    """A user message queued for a running run with ``AgentRuntime.steer``.

    It reaches the model at the run's next step boundary, after the current tool results and
    before the next model call, as a user message.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    text: str = Field(min_length=1, max_length=MAX_STEERING_CHARS)
    queued_at: datetime = Field(default_factory=utc_now)


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    request: RunRequest
    agent_key: str | None = None
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    status: RunStatus = RunStatus.RECEIVED
    step_count: int = 0
    continuation_count: int = 0
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
    tool_approvals: dict[str, ToolApproval] = Field(
        default_factory=dict,
        description="The decided approval of each approved call, by call id, bound to the "
        "tool and the arguments the approver approved. A call approved before 0.2 has none.",
    )
    # Tool guards, checkpointed so they survive an approval pause and a restart.
    tool_refusals: dict[str, int] = Field(
        default_factory=dict,
        description="Calls refused per tool (precheck or input schema): since the tool's last "
        "successful call, or in the whole run with tool_policy.refusal_count='per_run'.",
    )
    tool_call_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Calls run per tool named in tool_policy.max_calls_per_tool.",
    )
    seen_read_calls: set[str] = Field(
        default_factory=set,
        description="Digests of read-only calls run since the last successful state-changing "
        "call, kept when tool_policy.repeat_guard is on.",
    )
    pending_steering: list[SteeringMessage] = Field(
        default_factory=list,
        description="Steering messages not yet delivered to the model. Checkpointed, so they "
        "survive a pause and a restart; on an ended run, the ones that were never delivered.",
    )
    output_text: str | None = None
    rejected_output: str | None = Field(
        default=None,
        description="On a run that failed verification, the last answer the verifiers "
        "rejected, for diagnostics. It was never delivered: ``output_text`` is empty.",
    )
    error: str | None = None
    pause_payload: dict[str, Any] | None = None
    citations: list[Citation] = Field(default_factory=list)
    summary: ExecutionSummary = Field(default_factory=ExecutionSummary)
    # The run's first agent.run span ({"trace_id", "span_id"} as hex), so a run resumed after a
    # pause continues the same trace.
    trace_parent: dict[str, str] | None = None
    version: int = 0

    @model_serializer(mode="wrap")
    def _omit_empty_steering(self, handler: SerializerFunctionWrapHandler) -> Any:
        # 0.1.0a17 validates stored run state with extra="forbid" and has no pending_steering:
        # leave it out while it is empty, so a17 replicas can still load what a18 checkpoints
        # during a rolling deploy or after a rollback.
        # tool_approvals (0.2) is left out while empty for the same reason.
        data = handler(self)
        if isinstance(data, dict) and not self.pending_steering:
            data.pop("pending_steering", None)
        if isinstance(data, dict) and not self.tool_approvals:
            data.pop("tool_approvals", None)
        # rejected_output (0.2.0b2) is left out while unset for the same reason.
        if isinstance(data, dict) and self.rejected_output is None:
            data.pop("rejected_output", None)
        return data

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
    output: str | None = Field(
        default=None,
        description="The delivered answer. Set only on a completed run: a run that failed, "
        "timed out or was cancelled has none.",
    )
    error: str | None = None
    rejected_output: str | None = Field(
        default=None,
        # Read it as an attribute; it is left out of model_dump()/JSON, so a result passed on
        # whole (a sub-agent's tool result, say) never carries text that failed verification.
        exclude=True,
        description="On a run that failed verification, the last answer the verifiers "
        "rejected, for diagnostics only. Do not show it to the user as the answer. Left out "
        "of model_dump() and JSON.",
    )
    artifacts: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    citations: tuple[Citation, ...] = ()
    warnings: tuple[str, ...] = ()
    confidence: float | None = None
    execution_summary: ExecutionSummary
    undelivered_steering: tuple[SteeringMessage, ...] = Field(
        default=(),
        description="On an ended run, the steering messages it ended before delivering, for "
        "the host to hand back to the user.",
    )
