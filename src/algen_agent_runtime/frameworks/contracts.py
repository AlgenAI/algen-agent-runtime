from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.types.contracts import TokenUsage


class FrameworkRunStatus(StrEnum):
    COMPLETED = "completed"
    AWAITING_INPUT = "awaiting_input"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FrameworkEventType(StrEnum):
    STARTED = "started"
    DELTA = "delta"
    STEP = "step"
    TOOL = "tool"
    COMPLETED = "completed"
    FAILED = "failed"


class FrameworkCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    streaming: bool = False
    cancellation: bool = False
    persistence: bool = False
    human_in_the_loop: bool = False
    multi_agent: bool = False


class FrameworkRunRequest(BaseModel):
    """Provider-neutral input passed to an externally managed agent framework."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: str
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    context: dict[str, Any] = Field(default_factory=dict)


class FrameworkRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output: str
    status: FrameworkRunStatus = FrameworkRunStatus.COMPLETED
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metadata: dict[str, Any] = Field(default_factory=dict)
    resume_state: Any | None = Field(default=None, exclude=True)


class FrameworkStreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: FrameworkEventType
    delta: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    result: FrameworkRunResult | None = None


@runtime_checkable
class FrameworkAdapter(Protocol):
    @property
    def framework_id(self) -> str: ...

    @property
    def capabilities(self) -> FrameworkCapabilities: ...

    async def invoke(self, request: FrameworkRunRequest) -> FrameworkRunResult: ...

    def stream(self, request: FrameworkRunRequest) -> AsyncIterator[FrameworkStreamEvent]: ...

    async def cancel(self, run_id: str) -> bool: ...


class FrameworkAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, FrameworkAdapter] = {}

    def register(self, adapter: FrameworkAdapter) -> None:
        if adapter.framework_id in self._adapters:
            raise ValueError(f"framework adapter {adapter.framework_id!r} is already registered")
        self._adapters[adapter.framework_id] = adapter

    def get(self, framework_id: str) -> FrameworkAdapter:
        try:
            return self._adapters[framework_id]
        except KeyError as exc:
            raise KeyError(f"framework adapter {framework_id!r} is not registered") from exc

    def has(self, framework_id: str) -> bool:
        return framework_id in self._adapters

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def capabilities(self) -> Mapping[str, FrameworkCapabilities]:
        return {name: adapter.capabilities for name, adapter in self._adapters.items()}
