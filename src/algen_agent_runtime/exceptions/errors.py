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
