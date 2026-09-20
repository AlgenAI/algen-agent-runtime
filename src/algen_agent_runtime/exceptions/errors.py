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


class VerificationError(AlgenAgentRuntimeError):
    error_kind = ErrorKind.INVALID_RESPONSE


class RunPaused(AlgenAgentRuntimeError):
    pass
