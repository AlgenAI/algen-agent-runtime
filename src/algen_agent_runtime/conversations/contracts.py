from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from algen_agent_runtime.conversations.visualization import validate_vega_lite_specification
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
    outcome: Literal["completed", "failed", "blocked", "awaiting_clarification"] = "completed"


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


class ConversationStore(Protocol):
    async def create(self, conversation: Conversation) -> None: ...
    async def get(self, conversation_id: str, tenant_id: str) -> Conversation | None: ...
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
