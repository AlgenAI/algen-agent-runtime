from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Literal

from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan

TraceLevel = Literal["minimal", "standard", "detailed"]

_LEVEL_ORDER: dict[TraceLevel, int] = {
    "minimal": 0,
    "standard": 1,
    "detailed": 2,
}

_STANDARD_SPANS = frozenset({"agent.context.build", "agent.memory.write"})
_DETAILED_SPANS = frozenset({"agent.planning"})


def required_trace_level(span_name: str) -> TraceLevel:
    """Return the detail level required for a runtime-owned span.

    ``minimal`` keeps the decision chain: the run, each step, model and tool calls, one policy
    summary per checkpoint, verification, and guardrails that fired. ``standard`` adds context
    building and memory writes; ``detailed`` adds planning, cache operations and every guardrail
    evaluation, fired or not.
    """
    if span_name.startswith("cache.") or span_name in _DETAILED_SPANS:
        return "detailed"
    if span_name in _STANDARD_SPANS:
        return "standard"
    return "minimal"


def trace_level_includes(configured: TraceLevel, required: TraceLevel) -> bool:
    return _LEVEL_ORDER[configured] >= _LEVEL_ORDER[required]


class TraceLevelTracer:
    """Filter runtime-owned spans while preserving the active parent context."""

    def __init__(self, tracer: Any, level: TraceLevel) -> None:
        self._tracer = tracer
        self._level = level

    def start_as_current_span(self, name: str, *args: Any, **kwargs: Any) -> Any:
        required = required_trace_level(name)
        if trace_level_includes(self._level, required):
            return self._tracer.start_as_current_span(name, *args, **kwargs)
        return nullcontext(NonRecordingSpan(INVALID_SPAN_CONTEXT))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracer, name)
