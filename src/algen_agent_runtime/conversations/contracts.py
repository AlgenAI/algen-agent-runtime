from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from algen_agent_runtime.conversations.visualization import validate_vega_lite_specification
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.types.contracts import FileBlock, ImageBlock, Role, TextBlock, utc_now


class CodeBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["code"] = "code"
    language: str
    code: str
    title: str | None = None


class TableBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["table"] = "table"
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    truncated: bool = False


class ChartBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["chart"] = "chart"
    specification: dict[str, Any]
    grammar: Literal["vega-lite"] = "vega-lite"
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)

    @field_validator("specification")
    @classmethod
    def validate_specification(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_vega_lite_specification(value)


class NoticeBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["notice"] = "notice"
    level: Literal["info", "warning", "error"] = "info"
    text: str


class DetailsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    type: Literal["details"] = "details"
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1_000)
    expanded: bool = False
    content: tuple[TextBlock | CodeBlock | TableBlock | NoticeBlock, ...]


ConversationContentBlock = Annotated[
    TextBlock
    | ImageBlock
    | FileBlock
    | CodeBlock
    | TableBlock
    | ChartBlock
    | NoticeBlock
    | DetailsBlock,
    Field(discriminator="type"),
]


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class MessageStatus(StrEnum):
    ACCEPTED = "accepted"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    AWAITING_APPROVAL = "awaiting_approval"
    """The turn's run paused for an approval; answer it with ``ConversationService.respond``."""
    AWAITING_INPUT = "awaiting_input"
    """The turn's run asked the user a question; answer it with ``ConversationService.respond``."""
    EXPIRED = "expired"
    """The turn waited for an approval or an answer that never came in time."""


AWAITING_MESSAGE_STATUSES = frozenset(
    {MessageStatus.AWAITING_APPROVAL, MessageStatus.AWAITING_INPUT}
)


class ConversationPendingAction(BaseModel):
    """What a paused turn waits for, as much as a UI needs to ask the user and no more.

    It carries the same minimised fields as the run's ``approval.required`` and
    ``clarification.required`` events: tool arguments only as ``redacted_parameters``, never raw,
    and never tool results or caller credentials. It is stored on the assistant message, so a
    restarted service finds the pending turn again.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["approval", "input"]
    """``approval`` for a decision on a proposed action, ``input`` for a clarification."""
    turn_id: str
    """The user message that started the turn; the resume routes take it as ``turn_id``."""
    run_id: str | None = None
    """The paused agent run, when a runtime run backs the turn."""
    approval_id: str | None = None
    """The approval request to decide (``kind="approval"``)."""
    approval_kind: str | None = None
    """``tool_approval`` for a tool call, ``execution_continuation`` for more budget."""
    tool: str | None = None
    """The tool the proposed action calls, when there is one."""
    proposed_action: str | None = None
    """A short, safe description of the action, such as ``Execute crm.update``."""
    side_effect_summary: str | None = None
    """What approving it does, as the approval request states it."""
    risk: str | None = None
    """The approval request's risk level."""
    expires_at: datetime | None = None
    """When the approval request expires; a turn answered later ends as ``expired``."""
    options: tuple[str, ...] = ()
    """The decisions accepted: some of ``approved``, ``rejected`` and ``modified``."""
    redacted_parameters: dict[str, Any] = Field(default_factory=dict)
    """The proposed arguments after redaction, exactly as the approval event exposes them."""
    parameters_schema: dict[str, Any] | None = None
    """The tool's input JSON schema, for a UI that offers ``modified`` parameters."""
    question: str | None = None
    """The question to answer (``kind="input"``)."""
    paused_at: datetime = Field(default_factory=utc_now)
    """When the turn paused."""


class ConversationApprovalDecision(BaseModel):
    """A user's decision on a turn that awaits approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    decision: Literal["approved", "rejected", "modified"]
    """Approve the action, reject it, or approve it with ``modified_parameters``."""
    modified_parameters: dict[str, Any] | None = None
    """The arguments to use instead; required for ``modified`` and only allowed with it."""
    reason: str | None = Field(default=None, max_length=1_000)
    """Why it was rejected; told to the model when the agent continues after a rejection."""

    @model_validator(mode="after")
    def _modified_needs_parameters(self) -> ConversationApprovalDecision:
        if self.decision == "modified" and self.modified_parameters is None:
            raise ValueError("a modified decision requires modified_parameters")
        if self.decision != "modified" and self.modified_parameters is not None:
            raise ValueError("modified_parameters are only allowed with a modified decision")
        return self


class ConversationInputAnswer(BaseModel):
    """A user's answer to a turn that awaits input (a clarification)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=100_000)
    """The answer, given to the paused run as its clarification."""

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("answer cannot be empty")
        return value.strip()


ConversationTurnResponse = ConversationApprovalDecision | ConversationInputAnswer


class PendingTurnExpiredError(ConflictError):
    """The paused turn can no longer be answered because its approval or wait expired."""


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    user_id: str
    handler: str = "runtime"
    agent: str
    title: str = "New conversation"
    status: ConversationStatus = ConversationStatus.ACTIVE
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    version: int = 0


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    tenant_id: str
    role: Role
    content: tuple[ConversationContentBlock, ...]
    status: MessageStatus = MessageStatus.COMPLETED
    reply_to_message_id: str | None = None
    run_ids: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    citations: tuple[dict[str, Any], ...] = ()
    suggested_followups: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    pending: ConversationPendingAction | None = None
    """What the turn waits for while the message is ``awaiting_approval`` or ``awaiting_input``."""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def text(
        cls,
        *,
        conversation_id: str,
        tenant_id: str,
        role: Role,
        text: str,
        **values: Any,
    ) -> ConversationMessage:
        return cls(
            conversation_id=conversation_id,
            tenant_id=tenant_id,
            role=role,
            content=(TextBlock(text=text),),
            **values,
        )

    @property
    def text_content(self) -> str:
        return "\n".join(block.text for block in self.content if isinstance(block, TextBlock))


class ConversationTurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    content: tuple[ConversationContentBlock, ...]
    run_ids: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    citations: tuple[dict[str, Any], ...] = ()
    suggested_followups: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    outcome: Literal[
        "completed",
        "failed",
        "blocked",
        "awaiting_clarification",
        "awaiting_approval",
        "awaiting_input",
        "cancelled",
        "expired",
    ] = "completed"
    """How the turn ended or paused. ``awaiting_approval`` and ``awaiting_input`` pause it and
    need ``pending``; ``blocked`` and ``awaiting_clarification`` complete the message as before."""
    pending: ConversationPendingAction | None = None
    """What a paused turn waits for; set exactly when ``outcome`` is an awaiting one."""

    @model_validator(mode="after")
    def _pending_matches_outcome(self) -> ConversationTurnResult:
        awaiting = self.outcome in {"awaiting_approval", "awaiting_input"}
        if awaiting != (self.pending is not None):
            raise ValueError("pending is required for, and only allowed with, an awaiting outcome")
        return self


class ConversationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    conversation_id: str
    tenant_id: str
    message_id: str | None = None
    sequence: int = Field(ge=1)
    timestamp: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)


DeltaEmitter = Callable[[str, dict[str, Any] | None], Awaitable[None]]


class ConversationHandler(Protocol):
    @property
    def name(self) -> str: ...

    async def handle(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        history: Sequence[ConversationMessage],
        emit: DeltaEmitter,
    ) -> ConversationTurnResult: ...


class ResumableConversationHandler(ConversationHandler, Protocol):
    """A handler whose turns can pause for an approval or input and continue later.

    ``handle`` may return an ``awaiting_approval`` or ``awaiting_input`` result with ``pending``.
    The service stores ``pending`` on the assistant message, so these methods get everything they
    need from it, also after a restart. ``credentials`` are the caller's, kept in memory only.
    """

    async def resume(
        self,
        conversation: Conversation,
        pending: ConversationPendingAction,
        response: ConversationTurnResponse,
        *,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        """Apply the user's answer; raise ``ConflictError`` if the turn no longer waits."""
        ...

    async def follow(
        self,
        conversation: Conversation,
        pending: ConversationPendingAction,
        emit: DeltaEmitter,
    ) -> ConversationTurnResult:
        """Continue the resumed turn until it ends or pauses again."""
        ...

    async def settle(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> ConversationTurnResult:
        """The outcome of a pending turn whose work ended while it waited (expired, cancelled)."""
        ...

    async def pending_state(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> Literal["paused", "running", "ended"]:
        """Whether the work behind a pending turn still waits, runs again, or has ended."""
        ...

    async def cancel_pending(
        self, conversation: Conversation, pending: ConversationPendingAction
    ) -> None:
        """End the paused work behind a turn that is being cancelled."""
        ...


class ConversationStore(Protocol):
    async def create(self, conversation: Conversation) -> None: ...
    async def get(self, conversation_id: str, tenant_id: str) -> Conversation | None: ...
    async def delete(self, conversation_id: str, tenant_id: str) -> bool: ...
    async def list(
        self, tenant_id: str, user_id: str, limit: int, before: datetime | None = None
    ) -> Sequence[Conversation]: ...
    async def save(self, conversation: Conversation, expected_version: int) -> None: ...
    async def append_message(self, message: ConversationMessage) -> None: ...
    async def update_message(self, message: ConversationMessage) -> None: ...
    async def messages(
        self, conversation_id: str, tenant_id: str, limit: int, before: datetime | None = None
    ) -> Sequence[ConversationMessage]: ...
    async def pending_messages(self, limit: int = 1000) -> Sequence[ConversationMessage]: ...


class ConversationEventPublisher(Protocol):
    async def next_sequence(self, conversation_id: str) -> int: ...
    async def publish(self, event: ConversationEvent) -> None: ...
    async def history(
        self, conversation_id: str, after: int = 0
    ) -> Sequence[ConversationEvent]: ...

    def subscribe(
        self, conversation_id: str, after: int = 0
    ) -> AsyncIterator[ConversationEvent]: ...
