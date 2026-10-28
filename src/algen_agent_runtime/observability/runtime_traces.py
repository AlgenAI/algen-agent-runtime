"""Send a tracing backend only the traces the Runtime started.

With Traccia on, the Runtime makes Traccia's tracer provider the process-global one, because its own
components create spans with ``trace.get_tracer(...)``. Every other library that traces through the
global provider (FastAPI's request tracing, an httpx or SQLAlchemy instrumentor) then reaches the
same exporters, with no agent identity. ``RuntimeTraceFilterProcessor`` sits in front of the export
processors and forwards a trace only when the span that opened it in this process is a Runtime
root: a span marked ``algen.runtime.root`` (``agent.run``, ``conversation.turn``) or one created by
a Runtime tracer (instrumentation scope ``algen_agent_runtime.*``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context, create_key, get_value, set_value
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from algen_agent_runtime.observability.run_export import (
    RunScopedExportProcessor,
    _is_export_processor,
)

RUNTIME_ROOT_ATTRIBUTE = "algen.runtime.root"
RUNTIME_SCOPE_PREFIX = "algen_agent_runtime"
_RUNTIME_TRACE_KEY = create_key("algen.runtime.trace")


def _is_runtime_scope(name: str | None) -> bool:
    return name is not None and (
        name == RUNTIME_SCOPE_PREFIX or name.startswith(f"{RUNTIME_SCOPE_PREFIX}.")
    )


def runtime_trace_context(context: Context | None = None) -> Context:
    """``context`` marked as belonging to a trace the Runtime opened."""
    return set_value(_RUNTIME_TRACE_KEY, True, context)


def in_runtime_trace(context: Context | None = None) -> bool:
    """Whether the current span belongs to the Runtime: inside one of its roots, or one of its
    own spans."""
    if get_value(_RUNTIME_TRACE_KEY, context) is True:
        return True
    span = trace.get_current_span(context)
    scope = getattr(span, "instrumentation_scope", None)
    return span.is_recording() and _is_runtime_scope(getattr(scope, "name", None))


def is_runtime_root(span: ReadableSpan) -> bool:
    """Whether a trace's first span in this process makes the trace the Runtime's."""
    attributes = span.attributes or {}
    if attributes.get(RUNTIME_ROOT_ATTRIBUTE) is True:
        return True
    scope = span.instrumentation_scope
    return _is_runtime_scope(scope.name if scope is not None else None)


@dataclass
class _TraceVerdict:
    export: bool
    open_spans: int = 0


class RuntimeTraceFilterProcessor(SpanProcessor):
    """Forwards the spans of traces the Runtime opened and drops the rest.

    The first span of a trace started in this process decides for the whole trace, including
    spans that end after it. A trace is forgotten once all of its spans have ended.
    """

    def __init__(
        self,
        processors: Sequence[SpanProcessor],
        *,
        accept: Callable[[ReadableSpan], bool] = is_runtime_root,
    ) -> None:
        if not processors:
            raise ValueError("RuntimeTraceFilterProcessor needs at least one span processor")
        self._processors = tuple(processors)
        self._accept = accept
        self._lock = threading.Lock()
        self._traces: dict[int, _TraceVerdict] = {}

    @property
    def processors(self) -> tuple[SpanProcessor, ...]:
        return self._processors

    def _exports(self, trace_id: int) -> bool:
        with self._lock:
            verdict = self._traces.get(trace_id)
        # A span started before the filter was installed has no verdict; keep it.
        return verdict is None or verdict.export

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        context = span.context
        if context is None:
            return
        with self._lock:
            verdict = self._traces.get(context.trace_id)
            if verdict is None:
                verdict = _TraceVerdict(export=self._accept(span))
                self._traces[context.trace_id] = verdict
            verdict.open_spans += 1
            export = verdict.export
        if export:
            for processor in self._processors:
                processor.on_start(span, parent_context=parent_context)

    def _on_ending(self, span: Span) -> None:
        context = span.context
        if context is None or not self._exports(context.trace_id):
            return
        for processor in self._processors:
            hook = getattr(processor, "_on_ending", None)
            if hook is not None:
                hook(span)

    def on_end(self, span: ReadableSpan) -> None:
        context = span.context
        if context is None:
            return
        with self._lock:
            verdict = self._traces.get(context.trace_id)
            export = verdict is None or verdict.export
            if verdict is not None:
                verdict.open_spans -= 1
                if verdict.open_spans <= 0:
                    del self._traces[context.trace_id]
        if export:
            for processor in self._processors:
                processor.on_end(span)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return all(processor.force_flush(timeout_millis) for processor in self._processors)

    def shutdown(self) -> None:
        for processor in self._processors:
            processor.shutdown()


def install_runtime_trace_filter(provider: Any) -> RuntimeTraceFilterProcessor | None:
    """Put a ``RuntimeTraceFilterProcessor`` in front of a TracerProvider's export processors.

    Wraps the ``RunScopedExportProcessor`` when one is installed, otherwise the export processors
    themselves. Installs once per provider and returns the processor; returns None when there is
    nothing to wrap. Processors added later, such as a second OTLP exporter, are not filtered.
    """
    active = getattr(provider, "_active_span_processor", None)
    processors = tuple(getattr(active, "_span_processors", ()))
    for processor in processors:
        if isinstance(processor, RuntimeTraceFilterProcessor):
            return processor
    wrapped = [
        processor
        for processor in processors
        if isinstance(processor, RunScopedExportProcessor) or _is_export_processor(processor)
    ]
    if active is None or not wrapped:
        return None
    filtered = RuntimeTraceFilterProcessor(wrapped)
    replaced: list[Any] = []
    for processor in processors:
        if processor is wrapped[0]:
            replaced.append(filtered)
        elif processor not in wrapped:
            replaced.append(processor)
    active._span_processors = tuple(replaced)
    return filtered
