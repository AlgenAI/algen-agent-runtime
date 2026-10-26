from __future__ import annotations

from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, set_span_in_context

from algen_agent_runtime.observability.run_export import (
    RunScopedExportProcessor,
    install_run_scoped_export,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _provider(
    **options: object,
) -> tuple[TracerProvider, InMemorySpanExporter, RunScopedExportProcessor]:
    exporter = InMemorySpanExporter()
    scoped = RunScopedExportProcessor([SimpleSpanProcessor(exporter)], **options)  # type: ignore[arg-type]
    provider = TracerProvider()
    provider.add_span_processor(scoped)
    return provider, exporter, scoped


def _names(exporter: InMemorySpanExporter) -> list[str]:
    return [span.name for span in exporter.get_finished_spans()]


def test_a_run_is_exported_when_its_root_ends() -> None:
    provider, exporter, scoped = _provider()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("agent.run") as root:
        with tracer.start_as_current_span("agent.model.call"):
            pass
        with tracer.start_as_current_span("agent.tool.call"):
            with tracer.start_as_current_span("agent.policy.before_tool"):
                pass
        assert _names(exporter) == []
        context = root.get_span_context()
        assert scoped.span_count(context.trace_id, context.span_id) == 4

    assert _names(exporter) == [
        "agent.model.call",
        "agent.policy.before_tool",
        "agent.tool.call",
        "agent.run",
    ]
    assert scoped.span_count(context.trace_id, context.span_id) is None


def test_runs_are_held_separately() -> None:
    provider, exporter, _ = _provider()
    tracer = provider.get_tracer("test")
    first = tracer.start_span("agent.run")
    second = tracer.start_span("agent.run")
    with tracer.start_as_current_span("child", context=set_span_in_context(first)):
        pass

    second.end()
    assert len(exporter.get_finished_spans()) == 1
    first.end()
    assert len(exporter.get_finished_spans()) == 3


def test_the_size_cap_flushes_early_and_the_rest_follows_the_root() -> None:
    provider, exporter, _ = _provider(max_spans=2)
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("agent.run"):
        for _ in range(3):
            with tracer.start_as_current_span("step"):
                pass
        assert _names(exporter) == ["step", "step"]

    assert _names(exporter) == ["step", "step", "step", "agent.run"]


def test_the_age_cap_flushes_a_long_run() -> None:
    clock = Clock()
    provider, exporter, _ = _provider(max_seconds=60, clock=clock)
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("agent.run"):
        with tracer.start_as_current_span("early"):
            pass
        clock.now = 61
        with tracer.start_as_current_span("late"):
            pass
        assert _names(exporter) == ["early", "late"]

    assert _names(exporter) == ["early", "late", "agent.run"]


def test_a_resumed_run_opens_a_new_segment_in_the_same_trace() -> None:
    provider, exporter, _ = _provider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.run") as first:
        pass
    parent = NonRecordingSpan(first.get_span_context())

    with tracer.start_as_current_span("agent.run", context=set_span_in_context(parent)):
        with tracer.start_as_current_span("agent.model.call"):
            pass
        assert len(exporter.get_finished_spans()) == 1

    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    assert {span.context.trace_id for span in spans} == {first.get_span_context().trace_id}


def test_force_flush_releases_open_runs() -> None:
    provider, exporter, scoped = _provider()
    tracer = provider.get_tracer("test")
    root = tracer.start_span("agent.run")
    with tracer.start_as_current_span("child", context=set_span_in_context(root)):
        pass

    assert scoped.force_flush() is True
    assert _names(exporter) == ["child"]
    root.end()
    assert _names(exporter) == ["child", "agent.run"]


def test_install_wraps_export_processors_once_and_keeps_the_rest() -> None:
    provider = TracerProvider()
    enricher = SpanProcessor()
    batch = BatchSpanProcessor(InMemorySpanExporter())
    simple = SimpleSpanProcessor(InMemorySpanExporter())
    provider.add_span_processor(enricher)
    provider.add_span_processor(batch)
    provider.add_span_processor(simple)

    scoped = install_run_scoped_export(provider, max_spans=10)

    assert scoped is not None
    assert scoped.exporters == (batch, simple)
    assert provider._active_span_processor._span_processors == (enricher, scoped)
    assert install_run_scoped_export(provider) is scoped
    assert install_run_scoped_export(TracerProvider()) is None
    provider.shutdown()
