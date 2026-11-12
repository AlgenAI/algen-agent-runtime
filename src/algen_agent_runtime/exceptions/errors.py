from __future__ import annotations

from typing import TYPE_CHECKING as _TYPE_CHECKING

from algen_agent_runtime.types.contracts import ErrorKind

if _TYPE_CHECKING:
    from algen_agent_runtime.types.contracts import RunResult


class AlgenAgentRuntimeError(Exception):
    error_kind = ErrorKind.UNKNOWN
    retryable = False


class ConfigurationError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_REQUEST


class NotFoundError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_REQUEST


class ConflictError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_REQUEST


class RunConflictError(ConflictError):
    """A run's checkpoint changed since this writer read it (its optimistic version moved on).

    Raised by the run stores' ``save``. A run drive that gets it stands down without touching
    the run: another drive (a cancel, another process's recovery) has taken it over.
    """


class StoreUnavailableError(AlgenAgentRuntimeError):
    """A durable store (PostgreSQL, Redis) could not be reached or did not answer in time.

    Raised in place of the driver's own connection, timeout and server-shutdown errors. The
    store may be back on the next call: connection pools reconnect without a restart. When it
    interrupts a write, the write may or may not have been applied. The API answers 503.
    """

    error_kind = ErrorKind.UNAVAILABLE
    retryable = True


class PolicyDeniedError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.AUTHORIZATION


class ApprovalMismatchError(PolicyDeniedError):
    """A call was refused because its approval does not cover it.

    ``code`` says why:

    - ``approval.tool_mismatch``: the approval is for another tool, or another version of it.
    - ``approval.arguments_mismatch``: the call's arguments differ from the approved ones.
    - ``approval.arguments_unbound``: a workflow tool step reads values that did not exist yet
      when its approval was decided, so no approver saw them.
    - ``approval.modification_ignored``: an approver modified the parameters, and the workflow
      tool step does not read them.
    - ``approval.tool_changed``: the tool's definition or code changed after the approval was
      asked for (its fingerprint differs).

    Nothing was recorded or run: the call is refused before the execution ledger.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class CapabilityError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_REQUEST


class UserDataUnsupportedError(CapabilityError):
    """A configured store cannot list or delete one user's data, so ``UserDataService`` cannot
    export or erase it. Raised before anything is read or deleted; the message names the store."""


class ProviderError(AlgenAgentRuntimeError):
    """A model provider call failed.

    ``status_code`` is the provider's HTTP status when there was one. The router uses it with
    ``error_kind`` to tell a request every provider would reject (400/422 ``invalid_request``,
    raised without falling back) from one only this provider rejects (for example a 404 for a
    model it does not serve), which falls back to the next candidate.
    """

    def __init__(
        self,
        message: str,
        kind: ErrorKind = ErrorKind.UNKNOWN,
        retryable: bool = False,
        *,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.error_kind = kind
        self.retryable = retryable
        self.status_code = status_code


class InvalidToolCallError(ProviderError):
    """A model's tool call did not carry a JSON object of arguments.

    Either the model wrote malformed JSON, or the output-token limit cut the call off
    (``cut_off``). It is a retryable ``invalid_response``: the runtime asks the model again
    within the agent's retry policy, and a cut-off call is retried with a higher output limit.
    The message names the tool and the parse error, never the arguments, which can hold user
    data.
    """

    def __init__(self, provider: str, tool: str, problem: str, *, cut_off: bool = False):
        if cut_off:
            message = (
                f"{provider} tool call {tool!r} was cut off by the output-token limit "
                "(max_output_tokens)"
            )
        else:
            message = f"{provider} returned invalid arguments for tool {tool!r}: {problem}"
        super().__init__(message, ErrorKind.INVALID_RESPONSE, True)
        self.provider = provider
        self.tool = tool
        self.cut_off = cut_off


class ToolExecutionError(AlgenAgentRuntimeError):
    pass


class ToolNotAttemptedError(ToolExecutionError):
    """A tool refused a call before sending anything (no credential, an unsafe argument).

    The call had no effect, so the executor records it ``failed``, never ``indeterminate``, even
    for a tool that writes: there is nothing for an operator to reconcile.
    """


class ToolCapacityError(ToolNotAttemptedError):
    """Every tool worker thread is busy and the queue is full: the call was refused unstarted.

    Nothing ran, so the call is recorded ``failed``, never ``indeterminate``.
    """

    def __init__(self, max_workers: int, max_queued: int) -> None:
        self.max_workers = max_workers
        self.max_queued = max_queued
        super().__init__(
            f"tool thread pool is full ({max_workers} running, {max_queued} queued); "
            "the call was not started, try again later"
        )

    def details(self) -> dict[str, object]:
        return {
            "code": "tool_capacity_exceeded",
            "max_workers": self.max_workers,
            "max_queued": self.max_queued,
        }


class ToolkitConfigurationError(ToolNotAttemptedError, ConfigurationError):
    """A toolkit's client could not open because of its configuration (a missing setting or
    credential, a missing extra). It is a ``ConfigurationError`` with the toolkit's own message,
    and its calls are recorded as not attempted."""


class BudgetExceededError(AlgenAgentRuntimeError):
    """A run reached one of its limits.

    ``limit`` names the limit (``execution_steps``, ``model_tokens``, ``estimated_cost``,
    ``model_calls``, ``response_chars`` or ``events``) when the run ended because of it; the
    run's ``run.failed`` event then carries ``reason: limit_exceeded`` and ``limit``.
    """

    def __init__(self, message: str, *, limit: str | None = None) -> None:
        super().__init__(message)
        self.limit = limit


class QuotaExceededError(AlgenAgentRuntimeError):
    """A write would take a tenant past its storage quota.

    Nothing was written. ``resource`` is ``artifacts``, ``memory`` or ``learned_memory``;
    ``dimension`` is ``items`` or ``bytes``; ``used`` is the tenant's usage the write would have
    reached. The API answers 413 with ``code: quota_exceeded``.
    """

    error_kind = ErrorKind.INVALID_REQUEST

    def __init__(self, resource: str, dimension: str, limit: int, used: int) -> None:
        super().__init__(
            f"storage quota exceeded: {resource} {dimension} would reach {used} of a {limit} limit"
        )
        self.resource = resource
        self.dimension = dimension
        self.limit = limit
        self.used = used

    def details(self) -> dict[str, object]:
        return {
            "code": "quota_exceeded",
            "resource": self.resource,
            "dimension": self.dimension,
            "limit": self.limit,
            "used": self.used,
        }


class MeterRefusedError(AlgenAgentRuntimeError):
    """A usage meter declined to reserve a model call (no credits left, a limit reached).

    Raised by a deployment's ``UsageMeter.reserve``. The runtime does not retry it, does not
    fall back to another model, and does not count it against provider health; the run fails
    with this message as its error.

    The router fills ``provider`` (its registration id) and ``model`` with the candidate it was
    reserving before the error leaves it, so the ``budget.refused`` event and audit can name
    the refused model. A meter does not need to set them.
    """

    provider: str | None = None
    model: str | None = None


class VerificationError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_RESPONSE


class RunPaused(AlgenAgentRuntimeError):
    pass


class EventStreamOverflowError(AlgenAgentRuntimeError):
    """A run event subscription fell too far behind to be served from the bus's history.

    ``last_sequence`` is the sequence of the last event the subscriber received: subscribe again
    with ``after=last_sequence`` (over HTTP, reconnect with ``Last-Event-ID``) to read on from a
    durable history. A subscription never skips an event silently: it delivers every event in
    order or ends with this error.
    """

    def __init__(self, run_id: str, last_sequence: int) -> None:
        super().__init__(
            f"event subscription to run {run_id!r} fell behind after event {last_sequence}; "
            f"subscribe again with after={last_sequence}"
        )
        self.run_id = run_id
        self.last_sequence = last_sequence


class MCPNotConnectedError(ToolExecutionError):
    """An OAuth MCP server has no usable connection for this tenant (or user).

    It carries what a UI needs to offer a Connect button, never a token: the server name, the
    token scope and the route that starts the authorization flow. A tool call that hits it
    returns a clean tool error to the model, whatever the agent's ``on_error``.
    """

    error_kind = ErrorKind.AUTHORIZATION

    def __init__(self, server: str, *, token_scope: str, start_path: str) -> None:
        self.server = server
        self.token_scope = token_scope
        self.start_path = start_path
        whose = "this user" if token_scope == "user" else "this tenant"
        super().__init__(
            f"MCP server {server!r} is not connected for {whose}; "
            "ask the user to connect it, then try again"
        )

    def details(self) -> dict[str, str]:
        """The UI-facing description: no token, no tenant data."""
        return {
            "code": "mcp_not_connected",
            "server": self.server,
            "token_scope": self.token_scope,
            "start_path": self.start_path,
        }


def runtime_error_type(exc: BaseException) -> str | None:
    """The class name ``RunResult.error_type`` records for ``exc``.

    It is the nearest class defined in this module (a host's subclass of
    ``ToolExecutionError`` is recorded as ``ToolExecutionError``), or ``None`` for an error that
    is not a runtime error.
    """
    for cls in type(exc).__mro__:
        if cls.__module__ == __name__ and issubclass(cls, AlgenAgentRuntimeError):
            return cls.__name__
    return None


def run_failure_error(result: RunResult, default_message: str | None = None) -> Exception:
    """The error to raise for a run that did not complete, keeping its type.

    A failed run raises the runtime error that ended it (``RunResult.error_type``): a
    ``VerificationError`` when its answer failed verification, a ``BudgetExceededError`` (with
    ``limit``) when it reached a limit, and so on. A run that timed out raises ``TimeoutError``;
    any other ends in ``RuntimeError``. The message is the run's ``error``, else
    ``default_message``.
    """
    status = getattr(result.status, "value", result.status)
    message = result.error or default_message or f"run {result.run_id} ended {status}"
    if status == "timed_out":
        return TimeoutError(message)
    found = globals().get(getattr(result, "error_type", None) or "")
    if isinstance(found, type) and issubclass(found, AlgenAgentRuntimeError):
        # The nearest class that takes just a message: some carry structured arguments.
        for cls in found.__mro__:
            if not issubclass(cls, AlgenAgentRuntimeError):
                break
            try:
                error = cls(message)
            except TypeError:
                continue
            if isinstance(error, BudgetExceededError):
                error.limit = getattr(result, "limit_reached", None)
            return error
    return RuntimeError(message)
