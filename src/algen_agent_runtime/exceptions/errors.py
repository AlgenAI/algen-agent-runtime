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


class PolicyDeniedError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.AUTHORIZATION


class CapabilityError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_REQUEST


class ProviderError(AlgenAgentRuntimeError):
    def __init__(self, message: str, kind: ErrorKind = ErrorKind.UNKNOWN, retryable: bool = False):
        super().__init__(message)
        self.error_kind = kind
        self.retryable = retryable


class ToolExecutionError(AlgenAgentRuntimeError):
    pass


class BudgetExceededError(AlgenAgentRuntimeError):
    pass


class MeterRefusedError(AlgenAgentRuntimeError):
    """A usage meter declined to reserve a model call (no credits left, a limit reached).

    Raised by a deployment's ``UsageMeter.reserve``. The runtime does not retry it, does not
    fall back to another model, and does not count it against provider health; the run fails
    with this message as its error.
    """


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
