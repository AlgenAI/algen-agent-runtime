"""Golden redaction through the Traccia adapter (the ``traccia`` extra), offline: the Runtime's
spans are created through Traccia's tracer and lifecycle, and what Traccia's enrichment
processors and its export processors receive (what its OTLP exporter would send) carries no
canary. The span shape is not pinned here: Traccia's enrichment adds attributes of its own that
change with its releases."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from golden_harness import (
    ALL_CANARIES,
    WORKING_DATA,
    Telemetry,
    assert_no_canary,
    capture_telemetry,
    leaky_mapping,
    leaky_text,
)
from golden_scenarios import (
    agent_definition,
    collect,
    container,
    raising,
    request,
    returning,
    settings,
    tool,
    tool_call,
)

from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.types.contracts import RunStatus

traccia = pytest.importorskip("traccia")
tracer_module = pytest.importorskip("traccia.tracer")


class _Enriched:
    """A Traccia enrichment processor that keeps each span as Traccia hands it over."""

    def __init__(self) -> None:
        self.spans: list[str] = []

    def on_end(self, span: Any) -> None:
        self.spans.append(
            json.dumps(
                {
                    "name": getattr(span, "name", ""),
                    "attributes": dict(getattr(span, "attributes", {}) or {}),
                    "events": list(getattr(span, "events", []) or []),
                    "status": str(getattr(span, "status_description", "") or ""),
                },
                default=str,
            )
        )

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout: float | None = None) -> None:
        return None


@pytest.fixture
def traccia_spans(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> Iterator[tuple[Telemetry, _Enriched, Any]]:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from algen_agent_runtime.config.settings import TracciaSettings
    from algen_agent_runtime.observability.traccia_adapter import TracciaObservabilityAdapter

    original = traccia.get_tracer_provider()
    provider = tracer_module.TracerProvider()
    enriched = _Enriched()
    exported = InMemorySpanExporter()
    provider.add_span_processor(enriched)
    provider.add_span_processor(SimpleSpanProcessor(exported))
    traccia.set_tracer_provider(provider)
    adapter = TracciaObservabilityAdapter(TracciaSettings(enabled=True), "golden")
    adapter._module = traccia
    adapter._started = True
    monkeypatch.setattr(
        "algen_agent_runtime.orchestration.container.observability_adapter",
        lambda settings, environment=None: adapter,
    )
    try:
        with capture_telemetry(monkeypatch, caplog, capsys) as captured:
            yield captured, enriched, exported
    finally:
        traccia.set_tracer_provider(original)
        adapter._started = False


def _exported_text(exported: Any) -> str:
    lines = []
    for span in exported.get_finished_spans():
        lines.append(span.name)
        lines.append(json.dumps(dict(span.attributes or {}), default=str))
        lines.append(str(span.status.description or ""))
        for event in span.events:
            lines.append(json.dumps(dict(event.attributes or {}), default=str))
    return "\n".join(lines)


async def test_traccia_spans_carry_no_canary_with_content_capture(
    traccia_spans: tuple[Telemetry, _Enriched, Any],
) -> None:
    telemetry, enriched, exported = traccia_spans
    built = container(
        settings(agent_definition(enabled_tools=["crm.lookup"]), include_content=True),
        [tool("crm.lookup", returning(leaky_mapping("tool result")))],
        [tool_call("crm.lookup", {"query": "customer 7"}), leaky_text("answer")],
    )

    result = await built.runtime.run(request(leaky_text("input")))

    assert result.status == RunStatus.COMPLETED, result.error
    sinks, _ = await collect(built, telemetry, [result.run_id])
    sinks["traccia_enriched"] = "\n".join(enriched.spans)
    sinks["traccia_exported"] = _exported_text(exported)
    assert "agent.run" in sinks["traccia_exported"]
    assert "[REDACTED_EMAIL]" in sinks["traccia_exported"]
    assert_no_canary(sinks, ALL_CANARIES, scenario="traccia content", allowed=WORKING_DATA)


@pytest.mark.parametrize("error", ["runtime_error", "tool_execution_error"])
async def test_traccia_spans_record_exceptions_masked(
    traccia_spans: tuple[Telemetry, _Enriched, Any], error: str
) -> None:
    telemetry, enriched, exported = traccia_spans
    exception: Exception = (
        RuntimeError(leaky_text("tool error"))
        if error == "runtime_error"
        else ToolExecutionError(leaky_text("tool error"))
    )
    built = container(
        settings(agent_definition(enabled_tools=["billing.charge"])),
        [tool("billing.charge", raising(exception))],
        [tool_call("billing.charge", {"query": "invoice 9"})],
    )

    result = await built.runtime.run(request("charge it"))

    assert result.status == RunStatus.FAILED
    sinks, _ = await collect(built, telemetry, [result.run_id])
    sinks["traccia_enriched"] = "\n".join(enriched.spans)
    sinks["traccia_exported"] = _exported_text(exported)
    # The exception event is on the exported OpenTelemetry span; traccia 0.1.29 does not copy
    # it into its own span's events (earlier releases did), so either sink may carry it.
    assert "exception" in sinks["traccia_enriched"] + sinks["traccia_exported"]
    assert "[REDACTED]" in sinks["traccia_exported"]
    assert_no_canary(sinks, ALL_CANARIES, scenario=f"traccia {error}")
