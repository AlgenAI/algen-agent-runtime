"""Runtime loggers that never hand a host's logging a raw exception or an unmasked credential.

``configure_logging`` (what ``algen-agent-runtime serve`` uses) installs
``security.redact_log_event``, which masks every log line. A host that embeds the Runtime keeps
its own structlog configuration, often structlog's default (``ConsoleRenderer``, which prints an
exception's traceback, and with ``rich`` installed its local variables) or one that sends
exceptions to an error tracker. The Runtime's own loggers protect themselves there without
changing that configuration: each event they log is masked before it reaches the host's
processors, as ``redact_log_event`` would mask it, and an exception is passed as its masked
traceback text (the ``exception`` field ``format_exc_info`` would have written) instead of the
exception object (0.2.0b5).

When the host's structlog processors include ``redact_log_event`` (``configure_logging`` does),
the events pass through unchanged, exception objects included, as before.
"""

from __future__ import annotations

import sys
from typing import Any

import structlog

from algen_agent_runtime.security.redaction import (
    redact,
    redact_error_text,
    redact_log_event,
    redact_text,
)

_LEVELS = ("debug", "info", "warning", "warn", "error", "critical", "fatal", "msg")


def host_masks_logs() -> bool:
    """Whether structlog's configured processors include ``redact_log_event``."""
    try:
        processors = structlog.get_config().get("processors", ())
    except Exception:
        return False
    return any(processor is redact_log_event for processor in processors)


def _exception_text(exc_info: Any) -> str | None:
    """The masked traceback for a structlog ``exc_info`` value (True, an exception or a
    ``sys.exc_info()`` tuple), or None when there is no exception."""
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        exc: BaseException | None = exc_info
    elif isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc = exc_info[1]
    else:
        exc = None
    if exc is None:
        return None
    import traceback

    return redact_error_text("".join(traceback.format_exception(exc)).rstrip())


def _masked(event: Any, fields: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    exc_info = fields.pop("exc_info", None)
    masked = redact(fields)
    if not isinstance(masked, dict):  # pragma: no cover - redact keeps a dict a dict
        masked = {}
    text = _exception_text(exc_info) if exc_info else None
    if text is not None:
        masked["exception"] = text
    for key in ("exception", "stack"):
        if isinstance(masked.get(key), str):
            masked[key] = redact_error_text(masked[key])
    return (redact_text(event) if isinstance(event, str) else event), masked


class RedactingLogger:
    """A structlog logger for a Runtime component (see the module's documentation)."""

    def __init__(self, logger: Any) -> None:
        self._logger = logger

    @property
    def wrapped(self) -> Any:
        return self._logger

    def bind(self, **values: Any) -> RedactingLogger:
        return RedactingLogger(self._logger.bind(**redact(values)))

    def new(self, **values: Any) -> RedactingLogger:
        return RedactingLogger(self._logger.new(**redact(values)))

    def unbind(self, *keys: str) -> RedactingLogger:
        return RedactingLogger(self._logger.unbind(*keys))

    def exception(self, event: Any = None, *args: Any, **fields: Any) -> Any:
        fields.setdefault("exc_info", True)
        return self._log("error", event, args, fields)

    def _log(self, method: str, event: Any, args: tuple[Any, ...], fields: dict[str, Any]) -> Any:
        if not host_masks_logs():
            event, fields = _masked(event, fields)
            args = tuple(redact(arg) for arg in args)
        return getattr(self._logger, method)(event, *args, **fields)

    def __getattr__(self, name: str) -> Any:
        if name in _LEVELS:

            def log(event: Any = None, *args: Any, **fields: Any) -> Any:
                return self._log(name, event, args, fields)

            return log
        return getattr(self._logger, name)


def runtime_logger(name: str) -> RedactingLogger:
    """``structlog.get_logger(name)`` for a Runtime component, wrapped in
    :class:`RedactingLogger`."""
    return RedactingLogger(structlog.get_logger(name))
