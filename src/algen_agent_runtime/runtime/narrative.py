"""A run told in order: what was asked, what the agent did, what it cost and how it ended.

Built from the run's stored state and event history, not from spans, so it is complete whatever a
tracing backend received. It names tools, argument keys, sizes, tokens and outcomes but carries no
prompt, answer or tool content, so it can be handed to an analyst or another agent as it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.types.contracts import RunState

EntryKind = Literal[
    "run",
    "context",
    "model",
    "tool",
    "approval",
    "clarification",
    "policy",
    "verification",
    "memory",
    "outcome",
]

# Events that add nothing to the story: streamed text, and the assessment before each compaction.
_SKIPPED = frozenset({"model.delta", "model.started", "context.compaction.assessed"})


class NarrativeEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    at: datetime
    kind: EntryKind
    summary: str
    step_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class RunNarrative(BaseModel):
    """One run, in order. ``entries`` follow the event sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    agent: str
    status: str
    started_at: datetime
    updated_at: datetime
    duration_ms: float
    input_chars: int
    output_chars: int | None
    error: str | None
    step_count: int
    model_calls: int
    tool_calls: int
    retries: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cache_write_tokens: int
    estimated_cost_usd: float | None = Field(
        description="From configured provider rates; None when the run's providers are unpriced."
    )
    trace_id: str | None = None
    entries: tuple[NarrativeEntry, ...] = ()


def _join(names: Sequence[Any]) -> str:
    return ", ".join(str(name) for name in names) if names else "none"


def _entry(event: RunEvent) -> NarrativeEntry | None:
    data = event.data
    kind: EntryKind
    match event.type:
        case "run.started":
            kind, summary = "run", f"Run started for {data.get('agent', 'the agent')}"
        case "context.retrieved":
            kind = "context"
            summary = (
                f"Context built: {data.get('message_count', 0)} messages, "
                f"{data.get('source_count', 0)} retrieved sources"
            )
            data = {key: data[key] for key in ("message_count", "source_count") if key in data}
        case "context.compacted":
            kind = "context"
            summary = f"Compacted {data.get('message_count', 0)} earlier messages"
        case "context.working_state.updated":
            kind, summary = "context", "Updated its working state"
        case "context.skills.updated":
            kind, summary = "context", f"Loaded skills ({data.get('count', 0)} active)"
        case "model.completed":
            kind = "model"
            tools = data.get("tool_names") or []
            requested = f"; requested tools: {_join(tools)}" if tools else ""
            summary = (
                f"Model {data.get('model')} answered in {round(float(data.get('latency_ms') or 0))}"
                f" ms (finish: {data.get('finish_reason')}){requested}"
            )
        case "model.retrying":
            kind, summary = (
                "model",
                f"Model call failed and is retried (attempt {data.get('next_attempt')})",
            )
        case "budget.refused":
            kind = "model"
            summary = f"Usage meter refused the model call: {data.get('reason') or 'refused'}"
        case "tool.started":
            kind = "tool"
            arguments = _join(data.get("argument_keys") or [])
            if data.get("status", "running") == "running":
                summary = f"Called {data.get('tool')} with arguments: {arguments}"
            else:
                summary = f"Asked for {data.get('tool')} with arguments: {arguments}"
        case "tool.completed":
            kind = "tool"
            result = f", {data['result_chars']} result chars" if "result_chars" in data else ""
            summary = f"{data.get('tool') or 'Tool'} {data.get('status', 'completed')}{result}"
        case "tool.refused":
            kind = "tool"
            summary = f"{data.get('tool') or 'Tool'} call refused before it ran ({data.get('reason_code')})"
        case "tool.repeated":
            kind = "tool"
            summary = f"{data.get('tool') or 'Tool'} call held back ({data.get('reason_code')})"
        case "tool.wrap_up":
            kind = "tool"
            summary = f"Told the model to wrap up: {data.get('tool_calls_left')} tool calls left"
        case "approval.required":
            kind = "approval"
            subject = data.get("tool") or data.get("reason") or data.get("kind") or "an action"
            summary = f"Waiting for approval: {subject}"
            data = {key: value for key, value in data.items() if key != "parameters"}
        case "run.continued":
            kind, summary = "approval", f"Continued ({data.get('mode')}: {data.get('reason')})"
        case "clarification.required":
            kind, summary = "clarification", "Asked the user to clarify"
            data = {}
        case "policy.evaluated":
            if data.get("action") == "allow" and not data.get("triggered_count"):
                return None
            kind = "policy"
            summary = f"Policy at {data.get('boundary')}: {data.get('action')} ({data.get('reason_code')})"
        case "verification.completed":
            kind = "verification"
            summary = "Verification passed" if data.get("passed") else "Verification failed"
        case "memory.written":
            kind, summary = "memory", f"Saved {data.get('item_count', 0)} items to session memory"
        case "memory.learned.updated":
            kind, summary = "memory", "Updated learned memory"
        case "run.completed":
            kind, summary = "outcome", "Run completed"
        case "run.failed":
            kind = "outcome"
            summary = f"Run failed: {data.get('reason') or data.get('error') or 'error'}"
        case "run.cancelled":
            kind, summary = "outcome", "Run cancelled"
        case _:
            return None
    return NarrativeEntry(
        at=event.timestamp, kind=kind, summary=summary, step_id=event.step_id, details=dict(data)
    )


def build_narrative(state: RunState, events: Sequence[RunEvent]) -> RunNarrative:
    """The narrative of ``state`` from its event history, in sequence order."""
    ordered = sorted(events, key=lambda event: event.sequence)
    entries = tuple(
        entry
        for entry in (_entry(event) for event in ordered if event.type not in _SKIPPED)
        if entry
    )
    usage = state.summary.usage
    started = ordered[0].timestamp if ordered else state.updated_at
    return RunNarrative(
        run_id=state.id,
        agent=state.agent_key or state.request.agent,
        status=state.status.value,
        started_at=started,
        updated_at=state.updated_at,
        duration_ms=round((state.updated_at - started).total_seconds() * 1000, 3),
        input_chars=len(state.request.input),
        output_chars=len(state.output_text) if state.output_text is not None else None,
        error=state.error,
        step_count=state.step_count,
        model_calls=state.summary.model_calls,
        tool_calls=state.summary.tool_calls,
        retries=state.summary.retries,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_tokens=usage.cached_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        estimated_cost_usd=usage.estimated_cost_usd if usage.estimated_cost_usd > 0 else None,
        trace_id=(state.trace_parent or {}).get("trace_id"),
        entries=entries,
    )
