from __future__ import annotations

from algen_agent_runtime.types.contracts import ErrorKind


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


class CapabilityError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_REQUEST


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


class ToolExecutionError(AlgenAgentRuntimeError):
    pass


class ToolNotAttemptedError(ToolExecutionError):
    """A tool refused a call before sending anything (no credential, an unsafe argument).

    The call had no effect, so the executor records it ``failed``, never ``indeterminate``, even
    for a tool that writes: there is nothing for an operator to reconcile.
    """


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
