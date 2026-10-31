from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)

from algen_agent_runtime.exceptions.errors import (
    ConfigurationError,
    NotFoundError,
    PolicyDeniedError,
)
from algen_agent_runtime.types.contracts import Artifact, RetryPolicy, RunAttachment, utc_now


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


# The ToolContext values a tool's result can depend on, which it declares in ``cache_context``.
CacheContextValue = Literal["tenant_id", "user_id", "session_id", "run_id", "step_id"]


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
    cache_context: frozenset[CacheContextValue] = Field(
        default=frozenset(),
        description="The ToolContext values the result depends on (a query bound to the "
        "caller's user_id, say). The tool-result cache keys a result on them too, whatever "
        "the cache policy's scope, so one caller is never served another's result. Empty keeps "
        "the key on the tool, version and arguments within the policy's scope.",
    )

    failure_field: str | None = Field(
        default=None,
        min_length=1,
        description="A field of the tool's result object that reports failure: a result whose "
        "field is ``false`` (``failure_field='ok'`` and ``{\"ok\": false, ...}``) is recorded "
        "and emitted as failed, and still goes back to the model as the call's result.",
    )

    def reports_failure(self, value: Any) -> bool:
        """Whether a result value reports a failure through ``failure_field``."""
        return (
            self.failure_field is not None
            and isinstance(value, Mapping)
            and value.get(self.failure_field) is False
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


# Bounds on a tool note: the whole note serialized, its parts, and notes per call.
MAX_TOOL_NOTE_BYTES = 16_384
MAX_TOOL_NOTE_TITLE_CHARS = 200
MAX_TOOL_NOTE_ITEMS = 50
MAX_TOOL_NOTE_ITEM_CHARS = 2_000
MAX_TOOL_NOTES_PER_CALL = 20


class ToolNote(BaseModel):
    """What a tool call did, for the user interface: written as a ``tool.note`` run event.

    A note never reaches the model. ``paths`` are files or resources the call touched,
    ``suggestions`` short follow-ups to offer the user, and ``data`` anything else a UI shows
    (JSON values). The whole note is at most ``MAX_TOOL_NOTE_BYTES`` serialized.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    title: str = Field(min_length=1, max_length=MAX_TOOL_NOTE_TITLE_CHARS)
    paths: tuple[str, ...] = Field(default=(), max_length=MAX_TOOL_NOTE_ITEMS)
    suggestions: tuple[str, ...] = Field(default=(), max_length=MAX_TOOL_NOTE_ITEMS)
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("paths", "suggestions")
    @classmethod
    def _bounded_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(item) > MAX_TOOL_NOTE_ITEM_CHARS for item in value):
            raise ValueError(f"each item is at most {MAX_TOOL_NOTE_ITEM_CHARS} characters")
        return value

    def payload(self) -> dict[str, Any]:
        """The note as event data; raises ``ValueError`` when it is too large or not JSON."""
        try:
            payload = self.model_dump(mode="json")
            encoded = json.dumps(payload, allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("a tool note's data must be JSON values") from exc
        if len(encoded) > MAX_TOOL_NOTE_BYTES:
            raise ValueError(f"a tool note is at most {MAX_TOOL_NOTE_BYTES} bytes serialized")
        return payload


NoteSink = Callable[[ToolNote], Awaitable[None]]


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
    # The run's session (RunRequest.session_id), when it has one.
    session_id: str | None = None
    # The files the run was given (RunRequest.attachments), by reference; read them with
    # read_attachment.
    attachments: tuple[RunAttachment, ...] = ()
    secrets: Mapping[str, str] = Field(default_factory=dict)
    credentials: Mapping[str, str] = Field(
        default_factory=lambda: _NO_CREDENTIALS, repr=False, exclude=True
    )
    # Where emit_note sends notes: set by the Runtime for a call inside a run.
    _note_sink: NoteSink | None = PrivateAttr(default=None)
    # Loads one of the run's attachments from the Runtime's artifact store, for the tenant.
    _attachment_loader: Callable[[str], Awaitable[Artifact | None]] | None = PrivateAttr(
        default=None
    )

    async def read_attachment(self, artifact_id: str) -> Artifact:
        """One of the run's attached files, with its bytes (to extract text or store it).

        Only the run's own attachments can be read, and only from the run's tenant.
        """
        if artifact_id not in {item.artifact_id for item in self.attachments}:
            raise PolicyDeniedError(f"artifact {artifact_id!r} is not attached to this run")
        if self._attachment_loader is None:
            raise ConfigurationError("this tool call has no artifact store to read from")
        artifact = await self._attachment_loader(artifact_id)
        if artifact is None:
            raise NotFoundError(f"attachment {artifact_id!r} is no longer available")
        return artifact

    async def emit_note(
        self,
        *,
        title: str,
        paths: Sequence[str] = (),
        suggestions: Sequence[str] = (),
        data: Mapping[str, Any] | None = None,
    ) -> ToolNote:
        """Tell the user interface what this call did, without telling the model.

        The Runtime writes it as a ``tool.note`` run event tagged with the call's step and call
        id. Raises ``ValueError`` for a note past its bounds (see ``ToolNote``) or past
        ``MAX_TOOL_NOTES_PER_CALL`` notes in one call. Outside a run (a test, or a tool run
        directly) the note is checked and returned, and nothing is emitted.
        """
        if isinstance(paths, str) or isinstance(suggestions, str):
            raise ValueError("paths and suggestions are sequences of strings, not one string")
        note = ToolNote(
            title=title, paths=tuple(paths), suggestions=tuple(suggestions), data=dict(data or {})
        )
        note.payload()
        if self._note_sink is not None:
            await self._note_sink(note)
        return note

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
    """What a call returned. A handler may return one instead of a bare value, to set
    ``failed`` or ``metadata``; its ``value`` is checked like any other result."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    value: Any
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifact_ids: tuple[str, ...] = ()
    redacted: bool = False
    failed: bool = Field(
        default=False,
        description="The tool ran and returned an expected failure as data for the model to "
        "correct (``ToolResult(value=..., failed=True)``, or ``ToolDefinition.failure_field``). "
        "The call is recorded and emitted as failed, the value still goes to the model, and the "
        "result is never cached. It is not retried: the call finished.",
    )

    @model_serializer(mode="wrap")
    def _omit_unset_failure(self, handler: SerializerFunctionWrapHandler) -> Any:
        # 0.1.0a17 validates stored results with extra="forbid" and has no ``failed``: leave it
        # out unless it is set, so ledger rows and cached results stay readable by a17 replicas
        # during a rolling deploy or after a rollback.
        data = handler(self)
        if isinstance(data, dict) and not self.failed:
            data.pop("failed", None)
        return data


class ToolExecutionStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    INDETERMINATE = "indeterminate"
    # An operator asked for the tool's compensation; it is running, or the process stopped while
    # it ran. It never runs again by itself: an operator settles it.
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"


# Statuses that still need an operator: the side effect may or may not have happened, or a
# compensation has not been confirmed. Retention keeps their runs.
UNRESOLVED_TOOL_EXECUTION_STATUSES = frozenset(
    {
        ToolExecutionStatus.STARTED,
        ToolExecutionStatus.INDETERMINATE,
        ToolExecutionStatus.COMPENSATING,
        ToolExecutionStatus.COMPENSATION_FAILED,
    }
)


class ToolExecutionResolution(BaseModel):
    """One decision about a recorded tool call, kept in its ledger record."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["resolve", "reconcile", "compensate"] = Field(
        description="``resolve``: an operator stated the outcome; ``reconcile``: the tool's "
        "``reconcile`` hook reported it; ``compensate``: the tool's compensation was requested."
    )
    previous_status: ToolExecutionStatus = Field(description="The status before this decision.")
    status: ToolExecutionStatus = Field(description="The status this decision set.")
    actor_id: str = Field(
        min_length=1,
        max_length=256,
        description="Who decided: the operator's user id, or ``runtime`` for a reconciliation "
        "the Runtime made while replaying a call.",
    )
    reason: str = Field(default="", max_length=1000, description="Why, in the actor's words.")
    result_reference: str | None = Field(
        default=None,
        max_length=512,
        description="An identifier in the external system (an order or message id) that "
        "records the outcome.",
    )
    at: datetime = Field(default_factory=utc_now, description="When the decision was recorded.")


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
    created_at: datetime | None = Field(
        default=None, description="When the call was first reserved, as the store records it."
    )
    updated_at: datetime | None = Field(
        default=None, description="When the status last changed, as the store records it."
    )
    resolutions: tuple[ToolExecutionResolution, ...] = Field(
        default=(),
        description="Operator, reconciliation and compensation decisions, oldest first.",
    )


class ToolExecutionStore(Protocol):
    async def begin(
        self, context: ToolContext, tool_name: str
    ) -> tuple[ToolExecutionRecord, bool]: ...

    async def complete(self, idempotency_key: str, result: ToolResult) -> None: ...
    async def fail(self, idempotency_key: str, error: str, *, indeterminate: bool) -> None: ...


@runtime_checkable
class ReconcilableToolExecutionStore(ToolExecutionStore, Protocol):
    """A ledger that operators can query and settle; both built-in stores implement it.

    ``complete`` and ``fail`` only change a ``started`` record, so a late outcome from a call
    still in flight never overwrites an operator's decision.
    """

    async def get(self, idempotency_key: str, tenant_id: str) -> ToolExecutionRecord | None:
        """One tenant's record, or None."""
        ...

    async def find(
        self,
        tenant_id: str,
        *,
        statuses: Collection[ToolExecutionStatus],
        run_id: str | None = None,
        tool_name: str | None = None,
        min_age: timedelta | None = None,
        limit: int = 100,
    ) -> Sequence[ToolExecutionRecord]:
        """A tenant's records in ``statuses``, least recently changed first.

        ``min_age`` keeps records whose status has not changed for at least that long.
        """
        ...

    async def transition(
        self, record: ToolExecutionRecord, *, expected: Collection[ToolExecutionStatus]
    ) -> ToolExecutionRecord | None:
        """Store ``record`` only if the stored one, in the same tenant, is in ``expected``.

        Returns the stored record, or None when it is missing or another change won.
        """
        ...


class ReconcileOutcome(BaseModel):
    """What a tool's ``reconcile`` hook learned from the external system."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["completed", "failed", "unknown"] = Field(
        description="``completed``: the effect happened; ``failed``: it did not and will not; "
        "``unknown``: the external system cannot tell yet."
    )
    value: Any = Field(
        default=None,
        description="The call's result when completed; it must fit the tool's output schema.",
    )
    result_reference: str | None = Field(
        default=None, max_length=512, description="The external system's id for the effect."
    )
    detail: str | None = Field(
        default=None,
        max_length=500,
        description="A short, content-free note for the ledger and audit (no arguments, no "
        "secrets).",
    )


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[Any] | Any]
CompensationHandler = Callable[[ToolResult, ToolContext], Awaitable[None] | None]
ReplayValidator = Callable[[ToolResult, ToolContext], Awaitable[None] | None]
# Ask the external system, by the record's idempotency key, whether the call took effect.
Reconciler = Callable[
    [ToolExecutionRecord, ToolContext], Awaitable[ReconcileOutcome] | ReconcileOutcome
]
# A call in the same model reply as the one being checked that will run before it and has not
# run yet, as (definition, arguments).
EarlierCall = tuple[ToolDefinition, Mapping[str, Any]]
# Why a call would be refused, or None. It runs before any approval is asked for, so a call that
# cannot succeed goes back to the model instead of to the user. ``earlier`` holds the calls of the
# same reply that will run before this one and have not run yet: judge the call as if they had.
# When a reply's calls run one at a time, each earlier call has already run when the next is
# checked, so ``earlier`` is () and the precheck sees their effect in the system itself; only a
# reply whose calls run together (``tool_policy.parallel_calls``) fills it. Calls the precheck
# refused, or a guard held back, are left out, since they will not run.
Precheck = Callable[
    [ToolDefinition, Mapping[str, Any], Sequence[EarlierCall]],
    Awaitable[str | None] | str | None,
]


# Takes a tool's handler and returns the handler to run instead, usually one that calls it.
ToolMiddleware = Callable[[ToolHandler], ToolHandler]


class Tool:
    def __init__(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
        compensation: CompensationHandler | None = None,
        *,
        replay_validator: ReplayValidator | None = None,
        precheck: Precheck | None = None,
        reconcile: Reconciler | None = None,
        reconcile_on_replay: bool = True,
    ) -> None:
        """``compensation`` undoes a call's effect and runs only when an operator asks for it
        (``ToolReconciliation.compensate``). ``reconcile`` asks the external system whether a
        call whose outcome is unknown took effect: an operator's auto-reconcile uses it, and so
        does the executor when it replays such a call, unless ``reconcile_on_replay`` is False.
        """
        self.definition = definition
        self.handler = handler
        self.compensation = compensation
        self.replay_validator = replay_validator
        self.precheck = precheck
        self.reconcile = reconcile
        self.reconcile_on_replay = reconcile_on_replay

    def with_handler(self, handler: ToolHandler) -> Tool:
        """A copy of this tool that runs ``handler``, keeping everything else.

        The copy keeps the definition and every hook (compensation, replay validator,
        precheck, reconcile, ``reconcile_on_replay``) and any other attribute, including ones a
        later release or a subclass adds, so a wrapper that decorates a handler never drops one.
        The original tool is unchanged.
        """
        replaced = copy.copy(self)
        replaced.handler = handler
        return replaced

    async def check(
        self, arguments: Mapping[str, Any], earlier: Sequence[EarlierCall] = ()
    ) -> str | None:
        """The precheck's problem with this call, judged after ``earlier``, or None.

        ``earlier`` holds the calls of the same reply that will run before this one and have not
        run yet (see ``Precheck``); it is () when they already ran.
        """
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

    async def reconcile_call(
        self, record: ToolExecutionRecord, context: ToolContext
    ) -> ReconcileOutcome:
        """The ``reconcile`` hook's answer; ``unknown`` when the tool has none."""
        if self.reconcile is None:
            return ReconcileOutcome(status="unknown")
        outcome = self.reconcile(record, context)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        return ReconcileOutcome.model_validate(outcome)

    async def compensate(self, result: ToolResult, context: ToolContext) -> None:
        """Run the ``compensation`` handler; callers check that the tool has one."""
        if self.compensation is None:
            raise TypeError(f"tool {self.definition.name} has no compensation")
        outcome = self.compensation(result, context)
        if inspect.isawaitable(outcome):
            await outcome

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        result = self.handler(arguments, context)
        if inspect.isawaitable(result):
            return await result
        return result


def wrap(tool: Tool, middleware: ToolMiddleware) -> Tool:
    """``tool`` with its handler decorated by ``middleware``, keeping every hook.

    ``wrap(tool, logged)`` is ``tool.with_handler(logged(tool.handler))``.
    """
    return tool.with_handler(middleware(tool.handler))
