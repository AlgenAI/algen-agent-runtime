"""Spans that never record an exception's raw text.

OpenTelemetry's ``start_as_current_span`` (and Traccia's spans) record an exception that leaves a
span as an ``exception`` event carrying ``str(exc)`` and the formatted traceback, and set the
span's status description to ``"<Type>: <message>"``. A tool, a provider or a policy can put a
credential or a customer's personal data in an exception's message, so the Runtime's spans record
it themselves instead: the same ``exception`` event (``exception.type``, ``exception.message``,
``exception.stacktrace``, ``exception.escaped``) and the same ``ERROR`` status, with the message
and the traceback masked by the ``secrets`` and ``pii`` guardrails' rules (0.2.0b5).

Every Runtime tracer is wrapped in :class:`RedactingTracer`; it works with any tracer (the
OpenTelemetry API, Traccia's, a host's own), because it records the exception inside the span and
re-raises it after the span has closed, so the tracer never sees the raw exception.
"""

from __future__ import annotations

import traceback
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from algen_agent_runtime.security.redaction import redact_error_text


def telemetry_text(text: str) -> str:
    """``text`` with credentials and personal identifiers masked, for telemetry: credentials as
    ``redact_text`` masks them, then emails, phone, Aadhaar and card numbers as ``redact_pii``
    does (``security.redact_error_text``)."""
    return redact_error_text(text)


def exception_log_text(exc: BaseException) -> str:
    """``"<Type>: <message>"`` and the formatted traceback of ``exc``, masked by
    :func:`telemetry_text`, for a log line that must not carry the raw exception."""
    return telemetry_text("".join(traceback.format_exception(exc)).rstrip())


def exception_attributes(exc: BaseException) -> dict[str, str]:
    """The ``exception`` event's message and stack trace, masked by :func:`telemetry_text`."""
    return {
        "exception.message": telemetry_text(str(exc)),
        "exception.stacktrace": telemetry_text("".join(traceback.format_exception(exc))),
    }


def exception_description(exc: BaseException) -> str:
    """A failed span's status description, ``"<Type>: <masked message>"``."""
    return f"{type(exc).__name__}: {telemetry_text(str(exc))}"


def record_exception(span: Any, exc: BaseException, *, status: bool = True) -> None:
    """Record ``exc`` on ``span`` as an ``exception`` event and (with ``status``) an ``ERROR``
    status, as the tracer would have, with its text masked by :func:`telemetry_text`."""
    recording = getattr(span, "is_recording", None)
    try:
        if callable(recording) and not recording():
            return
    except Exception:
        return
    attributes = exception_attributes(exc)
    description = exception_description(exc)
    # A Traccia span wraps an OpenTelemetry span; its own record_exception writes the raw text.
    inner = getattr(span, "_otel_span", None)
    try:
        if inner is not None:
            if isinstance(getattr(span, "events", None), list):
                span.add_event("exception", {"exception.type": type(exc).__name__, **attributes})
            inner.record_exception(exc, attributes=attributes)
            if not status:
                return
            status_type = type(getattr(span, "status", None))
            error = getattr(status_type, "ERROR", None)
            if error is not None:
                span.set_status(error, description)
            else:
                inner.set_status(Status(StatusCode.ERROR, description))
            return
        recorder = getattr(span, "record_exception", None)
        if recorder is not None:
            recorder(exc, attributes=attributes)
        if status:
            span.set_status(Status(StatusCode.ERROR, description))
    except Exception:
        # Telemetry never changes what the run does.
        return


@contextmanager
def redacting_scope(scope: AbstractContextManager[Any]) -> Iterator[Any]:
    """``scope`` (a span's context manager) with any exception recorded masked by
    :func:`record_exception` and re-raised once the span has closed."""
    error: Exception | None = None
    with scope as span:
        try:
            yield span
        except Exception as exc:
            error = exc
            record_exception(span, exc)
    if error is not None:
        raise error


class RedactingTracer:
    """A tracer whose spans record exceptions masked (see the module's documentation)."""

    def __init__(self, tracer: Any) -> None:
        self._tracer = tracer

    @property
    def wrapped(self) -> Any:
        return self._tracer

    def start_as_current_span(
        self, name: str, *args: Any, **kwargs: Any
    ) -> AbstractContextManager[Any]:
        return redacting_scope(self._tracer.start_as_current_span(name, *args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracer, name)


def redacting_tracer(tracer: Any) -> Any:
    """``tracer`` wrapped in :class:`RedactingTracer`, once."""
    return tracer if isinstance(tracer, RedactingTracer) else RedactingTracer(tracer)


def runtime_tracer(name: str) -> RedactingTracer:
    """``trace.get_tracer(name)`` for a Runtime component, wrapped in :class:`RedactingTracer`."""
    return RedactingTracer(trace.get_tracer(name))
