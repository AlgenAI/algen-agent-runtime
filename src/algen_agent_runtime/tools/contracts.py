from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    requires_approval: bool = Field(
        default=False,
        description="Always ask for the Runtime's approval before a call, whatever the agent's "
        "approval policy. It can only add approval, never waive it.",
    )
    volatile: bool = Field(
        default=False,
        description="Reads state that changes on its own (a job's status, the time), so the "
        "repeat guard never holds an identical call back.",
    )


class _ReadOnlyCredentials(Mapping[str, str]):
    """A caller's credentials: read-only, never shown by ``repr`` and never pickled."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values: Mapping[str, str] = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"<credentials: {len(self._values)} redacted>"

    def __copy__(self) -> _ReadOnlyCredentials:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _ReadOnlyCredentials:
        return self

    def __reduce__(self) -> tuple[Any, ...]:
        # Credentials live in memory only: a pickled copy comes back empty.
        return (_ReadOnlyCredentials, ({},))


_NO_CREDENTIALS = _ReadOnlyCredentials({})


def read_only_credentials(credentials: Mapping[str, str] | None) -> Mapping[str, str]:
    """A private, read-only copy of a caller's credentials.

    Raises ``TypeError``, naming no key or value, when it is not a mapping of strings to strings.
    """
    if credentials is None:
        return _NO_CREDENTIALS
    if isinstance(credentials, _ReadOnlyCredentials):
        # Already a private, immutable copy.
        return credentials
    if not isinstance(credentials, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in credentials.items()
    ):
        raise TypeError("credentials must be a mapping of strings to strings")
    return _ReadOnlyCredentials(credentials) if credentials else _NO_CREDENTIALS


class ToolContext(BaseModel):
    """What a tool knows about a call: who, which run and step, and what it may do.

    ``credentials`` are the calling user's credentials for this run (for example an
    ``authorization`` header to forward), as the host passed them to ``Runtime.run``, ``start`` or
    ``resume``. They are a read-only copy kept in memory only: left out of ``repr``,
    ``model_dump``, equality and hashing, and never written to run state, events, audits or spans.
    A run resumed without credentials, or recovered after a restart, has none.
    """

    # Validation errors never echo their input: it may hold secrets or credentials.
    model_config = ConfigDict(
        extra="forbid", frozen=True, arbitrary_types_allowed=True, hide_input_in_errors=True
    )
    run_id: str
    step_id: str
    tenant_id: str
    user_id: str
    permissions: frozenset[str]
    idempotency_key: str
    secrets: Mapping[str, str] = Field(default_factory=dict)
    credentials: Mapping[str, str] = Field(
        default_factory=lambda: _NO_CREDENTIALS, repr=False, exclude=True
    )

    @field_validator("credentials", mode="plain")
    @classmethod
    def _read_only_credentials(cls, value: Any) -> Mapping[str, str]:
        try:
            return read_only_credentials(value)
        except TypeError as exc:
            raise ValueError(str(exc)) from None

    def _compared(self) -> tuple[Any, ...]:
        return (
            self.run_id,
            self.step_id,
            self.tenant_id,
            self.user_id,
            self.permissions,
            self.idempotency_key,
            dict(self.secrets),
        )

    def __eq__(self, other: object) -> bool:
        # Credentials take no part, so comparing two contexts says nothing about them.
        if not isinstance(other, ToolContext):
            return NotImplemented
        return self._compared() == other._compared()

    def __hash__(self) -> int:
        return hash(
            (
                self.run_id,
                self.step_id,
                self.tenant_id,
                self.user_id,
                self.permissions,
                self.idempotency_key,
            )
        )


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
ReplayValidator = Callable[[ToolResult, ToolContext], Awaitable[None] | None]
# A call planned before the one being checked, in the same model reply: it runs first.
EarlierCall = tuple[ToolDefinition, Mapping[str, Any]]
# Why a call would be refused, or None. It runs before any approval is asked for, so a call that
# cannot succeed goes back to the model instead of to the user.
Precheck = Callable[
    [ToolDefinition, Mapping[str, Any], Sequence[EarlierCall]],
    Awaitable[str | None] | str | None,
]


class Tool:
    def __init__(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
        compensation: CompensationHandler | None = None,
        *,
        replay_validator: ReplayValidator | None = None,
        precheck: Precheck | None = None,
    ) -> None:
        self.definition = definition
        self.handler = handler
        self.compensation = compensation
        self.replay_validator = replay_validator
        self.precheck = precheck

    async def check(
        self, arguments: Mapping[str, Any], earlier: Sequence[EarlierCall] = ()
    ) -> str | None:
        """The precheck's problem with this call, judged after ``earlier``, or None."""
        if self.precheck is None:
            return None
        problem = self.precheck(self.definition, arguments, earlier)
        if inspect.isawaitable(problem):
            problem = await problem
        return problem

    async def validate_replay(self, result: ToolResult, context: ToolContext) -> None:
        if self.replay_validator is not None:
            validation = self.replay_validator(result, context)
            if inspect.isawaitable(validation):
                await validation

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        result = self.handler(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result
