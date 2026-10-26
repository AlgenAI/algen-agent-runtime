"""Export a run's spans together, so a tracing backend never receives part of a run.

OpenTelemetry's batch processor exports whatever spans have ended each time its timer fires, so
one agent run leaves in several batches and its root, which ends last, is always in the final
one. A backend that loses or rejects one batch then shows a run with a hole in it, or a batch of
child spans whose parent never arrives. ``RunScopedExportProcessor`` sits in front of the export
processors: it holds each trace's spans until the span that opened the trace in this process (the
run's root) ends, then hands them over in one go. Size and age caps bound what it holds.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

DEFAULT_MAX_SPANS = 2_000
DEFAULT_MAX_SECONDS = 300.0
# Export processors installed by tracing SDKs that wrap OpenTelemetry's own.
_EXPORT_PROCESSOR_NAMES = frozenset({"RateLimitingSpanProcessor"})


@dataclass
class _RunSegment:
    """The spans of one trace opened in this process, from its local root on."""

    root_span_id: int
    opened_at: float
    started: int = 0
    held: list[ReadableSpan] = field(default_factory=list)


class RunScopedExportProcessor(SpanProcessor):
    """Holds each trace's spans until its local root ends, then forwards them together.

    The local root is the first span this process starts in a trace: an ``agent.run``, or the
    resumed ``agent.run`` of a run continued from an earlier trace. A trace holding more than
    ``max_spans`` spans, or open longer than ``max_seconds``, is forwarded early so memory stays
    bounded; the remaining spans follow when the root ends.
    """

    def __init__(
        self,
        exporters: Sequence[SpanProcessor],
        *,
        max_spans: int = DEFAULT_MAX_SPANS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not exporters:
            raise ValueError("RunScopedExportProcessor needs at least one export processor")
        self._exporters = tuple(exporters)
        self._max_spans = max_spans
        self._max_seconds = max_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._segments: dict[int, _RunSegment] = {}

    @property
    def exporters(self) -> tuple[SpanProcessor, ...]:
        return self._exporters

    def span_count(self, trace_id: int, span_id: int) -> int | None:
        """Spans started so far in the segment ``span_id`` opened, or None if it opened none."""
        with self._lock:
            segment = self._segments.get(trace_id)
            if segment is None or segment.root_span_id != span_id:
                return None
            return segment.started

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        context = span.context
        if context is None:
            return
        with self._lock:
            segment = self._segments.get(context.trace_id)
            if segment is None:
                segment = _RunSegment(root_span_id=context.span_id, opened_at=self._clock())
                self._segments[context.trace_id] = segment
            segment.started += 1
        for exporter in self._exporters:
            exporter.on_start(span, parent_context=parent_context)

    def _on_ending(self, span: Span) -> None:
        for exporter in self._exporters:
            hook = getattr(exporter, "_on_ending", None)
            if hook is not None:
                hook(span)

    def on_end(self, span: ReadableSpan) -> None:
        context = span.context
        if context is None:
            return
        ready: list[ReadableSpan] = []
        now = self._clock()
        with self._lock:
            segment = self._segments.get(context.trace_id)
            if segment is None:
                ready.append(span)
            else:
                segment.held.append(span)
                if context.span_id == segment.root_span_id:
                    del self._segments[context.trace_id]
                    ready.extend(segment.held)
                elif len(segment.held) >= self._max_spans:
                    ready.extend(segment.held)
                    segment.held = []
            for other in self._segments.values():
                if other.held and now - other.opened_at >= self._max_seconds:
                    ready.extend(other.held)
                    other.held = []
                    other.opened_at = now
        self._forward(ready)

    def _forward(self, spans: Sequence[ReadableSpan]) -> None:
        for held in spans:
            for exporter in self._exporters:
                exporter.on_end(held)

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        with self._lock:
            ready = [span for segment in self._segments.values() for span in segment.held]
            for segment in self._segments.values():
                segment.held = []
        self._forward(ready)
        return all(exporter.force_flush(timeout_millis) for exporter in self._exporters)

    def shutdown(self) -> None:
        self.force_flush()
        for exporter in self._exporters:
            exporter.shutdown()


def _is_export_processor(processor: Any) -> bool:
    return isinstance(processor, (BatchSpanProcessor, SimpleSpanProcessor)) or (
        type(processor).__name__ in _EXPORT_PROCESSOR_NAMES
    )


def install_run_scoped_export(
    provider: Any,
    *,
    max_spans: int = DEFAULT_MAX_SPANS,
    max_seconds: float = DEFAULT_MAX_SECONDS,
) -> RunScopedExportProcessor | None:
    """Put a ``RunScopedExportProcessor`` in front of a TracerProvider's export processors.

    Installs once per provider and returns the processor; returns None when the provider has no
    export processor to wrap. Processors that only enrich or inspect spans keep their place.
    """
    active = getattr(provider, "_active_span_processor", None)
    processors = tuple(getattr(active, "_span_processors", ()))
    for processor in processors:
        if isinstance(processor, RunScopedExportProcessor):
            return processor
    exporters = [processor for processor in processors if _is_export_processor(processor)]
    if active is None or not exporters:
        return None
    scoped = RunScopedExportProcessor(exporters, max_spans=max_spans, max_seconds=max_seconds)
    replaced: list[Any] = []
    for processor in processors:
        if processor is exporters[0]:
            replaced.append(scoped)
        elif processor not in exporters:
            replaced.append(processor)
    active._span_processors = tuple(replaced)
    return scoped
