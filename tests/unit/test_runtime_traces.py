from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_runtime  # type: ignore[import-not-found]
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from algen_agent_runtime.config.settings import TracciaSettings, load_settings
from algen_agent_runtime.observability.run_export import RunScopedExportProcessor
from algen_agent_runtime.observability.runtime_traces import (
    RUNTIME_ROOT_ATTRIBUTE,
    RuntimeTraceFilterProcessor,
)
from algen_agent_runtime.observability.traccia_adapter import TracciaObservabilityAdapter
from algen_agent_runtime.observability.trace_levels import TraceLevelTracer
from algen_agent_runtime.types.contracts import RunRequest

FASTAPI_SCOPE = "opentelemetry.instrumentation.fastapi"


def _traccia(
    monkeypatch: pytest.MonkeyPatch, **settings: Any
) -> tuple[TracciaObservabilityAdapter, TracerProvider, InMemorySpanExporter]:
    """A started adapter over a fake Traccia SDK whose spans reach an in-memory exporter."""
    exporter = InMemorySpanExporter()
    otel_provider = TracerProvider()
    otel_provider.add_span_processor(SimpleSpanProcessor(exporter))
    fake = SimpleNamespace(
        init=lambda **options: SimpleNamespace(
            _otel_tracer_provider=otel_provider, add_span_processor=lambda processor: None
        ),
        get_tracer=otel_provider.get_tracer,
        # Like ``traccia.span``: the SDK's default tracer, not a Runtime scope.
        span=lambda name, attributes=None: otel_provider.get_tracer(
            "default"
        ).start_as_current_span(name, attributes=attributes),
        runtime_config=SimpleNamespace(run_identity=lambda **identity: nullcontext()),
    )
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: fake,
    )
    adapter = TracciaObservabilityAdapter(TracciaSettings(enabled=True, **settings), "svc")
    monkeypatch.setattr(adapter, "_register_otel_provider", lambda provider: None)
    adapter.start()
    return adapter, otel_provider, exporter


def _exported(exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in exporter.get_finished_spans()}


def _processors(provider: TracerProvider) -> tuple[Any, ...]:
    return tuple(provider._active_span_processor._span_processors)


def test_foreign_trace_is_dropped_and_runtime_root_is_exported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, provider, exporter = _traccia(monkeypatch)
    http = provider.get_tracer(FASTAPI_SCOPE)

    with http.start_as_current_span("GET /health"):
        with provider.get_tracer("opentelemetry.instrumentation.httpx").start_as_current_span(
            "GET upstream"
        ):
            pass
    scope = adapter.span_scope("conversation.turn", attributes={"span.type": "agent"}, root=True)
    assert scope is not None
    with scope:
        with adapter.tracer("algen_agent_runtime.conversations").start_as_current_span("child"):
            pass

    exported = _exported(exporter)
    assert set(exported) == {"conversation.turn", "child"}
    turn = exported["conversation.turn"]
    assert turn.attributes is not None and turn.attributes[RUNTIME_ROOT_ATTRIBUTE] is True
    assert turn.parent is None
    [processor] = _processors(provider)
    assert isinstance(processor, RuntimeTraceFilterProcessor)
    assert isinstance(processor.processors[0], RunScopedExportProcessor)


async def test_run_started_inside_a_request_is_exported_with_its_nested_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, provider, exporter = _traccia(monkeypatch)
    runtime = make_runtime()
    runtime.observability = adapter
    runtime._tracer = TraceLevelTracer(adapter.tracer("algen_agent_runtime.runtime"), "detailed")

    # The run's task inherits the request's span, as when a FastAPI handler starts it.
    with provider.get_tracer(FASTAPI_SCOPE).start_as_current_span("POST /v1/runs"):
        result = await runtime.run(
            RunRequest(agent="test-agent", input="hello", tenant_id="tenant", user_id="user")
        )

    assert result.output == "answer"
    spans = exporter.get_finished_spans()
    names = [span.name for span in spans]
    assert "POST /v1/runs" not in names
    assert {"agent.run", "agent.step", "agent.model.call"} <= set(names)
    run = next(span for span in spans if span.name == "agent.run")
    assert run.parent is None
    assert run.attributes is not None and run.attributes[RUNTIME_ROOT_ATTRIBUTE] is True
    assert run.context is not None
    assert {span.context.trace_id for span in spans if span.context} == {run.context.trace_id}
    assert all(
        span.attributes is not None and RUNTIME_ROOT_ATTRIBUTE not in span.attributes
        for span in spans
        if span is not run
    )


def test_export_foreign_traces_sends_everything_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, provider, exporter = _traccia(monkeypatch, export_foreign_traces=True)

    with provider.get_tracer(FASTAPI_SCOPE).start_as_current_span("POST /v1/runs") as request:
        scope = adapter.span_scope("agent.run", attributes={})
        assert scope is not None
        with scope:
            pass
    with provider.get_tracer(FASTAPI_SCOPE).start_as_current_span("GET /health"):
        pass

    exported = _exported(exporter)
    assert set(exported) == {"POST /v1/runs", "agent.run", "GET /health"}
    # The run stays a child of the request, as it was before the filter existed.
    assert exported["agent.run"].parent is not None
    assert exported["agent.run"].parent.span_id == request.get_span_context().span_id
    assert not any(isinstance(p, RuntimeTraceFilterProcessor) for p in _processors(provider))


def test_filter_without_whole_run_export_keeps_rootless_runtime_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, provider, exporter = _traccia(monkeypatch, export_whole_runs=False)
    [processor] = _processors(provider)
    assert isinstance(processor, RuntimeTraceFilterProcessor)
    assert isinstance(processor.processors[0], SimpleSpanProcessor)

    with provider.get_tracer(FASTAPI_SCOPE).start_as_current_span("GET /"):
        pass
    # A Runtime component span with no Runtime root, e.g. a model service called outside a run.
    with trace.get_tracer(
        "algen_agent_runtime.model_services", tracer_provider=provider
    ).start_as_current_span("model_service.invoke"):
        pass
    scope = adapter.span_scope("agent.run", attributes={})
    assert scope is not None
    with scope:
        pass

    assert set(_exported(exporter)) == {"model_service.invoke", "agent.run"}


def test_nested_runtime_root_joins_its_turn_and_resumed_run_keeps_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, provider, exporter = _traccia(monkeypatch)
    http = provider.get_tracer(FASTAPI_SCOPE)

    with http.start_as_current_span("POST /v1/conversations/c/messages"):
        turn_scope = adapter.span_scope("conversation.turn", attributes={}, root=True)
        assert turn_scope is not None
        with turn_scope as turn:
            run_scope = adapter.span_scope("agent.run", attributes={})
            assert run_scope is not None
            with run_scope:
                pass
        stored = SpanContext(
            trace_id=0x1234,
            span_id=0x5678,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        token = otel_context.attach(trace.set_span_in_context(NonRecordingSpan(stored)))
        try:
            resumed_scope = adapter.span_scope("agent.run.resumed", attributes={})
            assert resumed_scope is not None
            with resumed_scope:
                pass
        finally:
            otel_context.detach(token)

    exported = _exported(exporter)
    assert set(exported) == {"conversation.turn", "agent.run", "agent.run.resumed"}
    run = exported["agent.run"]
    assert run.parent is not None
    assert run.parent.span_id == turn.get_span_context().span_id
    assert run.attributes is not None and RUNTIME_ROOT_ATTRIBUTE not in run.attributes
    resumed = exported["agent.run.resumed"]
    assert resumed.parent is not None and resumed.parent.span_id == 0x5678
    assert resumed.context is not None and resumed.context.trace_id == 0x1234
    assert resumed.attributes is not None and resumed.attributes[RUNTIME_ROOT_ATTRIBUTE] is True


def test_span_ending_after_its_foreign_root_is_still_dropped() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(RuntimeTraceFilterProcessor([SimpleSpanProcessor(exporter)]))
    http = provider.get_tracer(FASTAPI_SCOPE)

    request = http.start_span("POST /v1/runs")
    background = http.start_span("background", context=trace.set_span_in_context(request))
    request.end()
    background.end()
    with provider.get_tracer("algen_agent_runtime.runtime").start_as_current_span("agent.run"):
        pass

    assert [span.name for span in exporter.get_finished_spans()] == ["agent.run"]


def test_export_foreign_traces_setting_from_yaml_and_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert TracciaSettings().export_foreign_traces is False
    config = tmp_path / "agent.yaml"
    config.write_text(
        "telemetry:\n  traccia:\n    enabled: true\n    export_foreign_traces: true\n",
        encoding="utf-8",
    )
    assert load_settings((config,)).telemetry.traccia.export_foreign_traces is True
    monkeypatch.setenv("ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__EXPORT_FOREIGN_TRACES", "false")
    assert load_settings((config,)).telemetry.traccia.export_foreign_traces is False
