"""OTLP metric export: a reader only when a metrics endpoint is configured."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.trace import NoOpTracerProvider
from pydantic import ValidationError
from structlog.testing import capture_logs

from algen_agent_runtime.config.settings import TelemetrySettings
from algen_agent_runtime.observability import setup
from algen_agent_runtime.observability.setup import (
    configure_telemetry,
    flush_telemetry,
    otlp_meter_provider,
)


@pytest.fixture
def installed(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Record provider installation instead of changing the process-global providers."""
    calls: dict[str, list[Any]] = {"tracer": [], "meter": []}
    monkeypatch.setattr(setup.trace, "set_tracer_provider", calls["tracer"].append)
    monkeypatch.setattr(setup.trace, "get_tracer_provider", NoOpTracerProvider)
    monkeypatch.setattr(setup.metrics, "set_meter_provider", calls["meter"].append)
    monkeypatch.setattr(setup.metrics, "get_meter_provider", NoOpMeterProvider)
    monkeypatch.setattr(setup, "_installed_meter_provider", None)
    yield calls
    for provider in calls["meter"]:
        provider.shutdown()
    for provider in calls["tracer"]:
        provider.shutdown()


def test_a_trace_endpoint_alone_installs_no_meter_provider(
    installed: dict[str, list[Any]],
) -> None:
    configure_telemetry(TelemetrySettings(otlp_endpoint="http://127.0.0.1:4318/v1/traces"))

    assert len(installed["tracer"]) == 1
    assert installed["meter"] == []


def test_a_metrics_endpoint_installs_a_periodic_otlp_reader(
    installed: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[str, int]] = []

    def build(endpoint: str, *, export_interval_millis: int) -> MeterProvider:
        built.append((endpoint, export_interval_millis))
        return otlp_meter_provider(endpoint, export_interval_millis=export_interval_millis)

    monkeypatch.setattr(setup, "otlp_meter_provider", build)
    configure_telemetry(
        TelemetrySettings(
            otlp_metrics_endpoint="http://127.0.0.1:4318/v1/metrics",
            otlp_metrics_export_interval_seconds=15,
        )
    )

    assert installed["tracer"] == []
    assert built == [("http://127.0.0.1:4318/v1/metrics", 15_000)]
    [provider] = installed["meter"]
    assert isinstance(provider, MeterProvider)


def test_disabled_telemetry_or_an_existing_provider_is_left_alone(
    installed: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = "http://127.0.0.1:4318/v1/metrics"
    configure_telemetry(TelemetrySettings(enabled=False, otlp_metrics_endpoint=endpoint))
    assert installed["meter"] == []

    existing = MeterProvider()
    monkeypatch.setattr(setup.metrics, "get_meter_provider", lambda: existing)
    configure_telemetry(TelemetrySettings(otlp_metrics_endpoint=endpoint))

    assert installed["meter"] == []
    existing.shutdown()


def test_the_export_interval_is_bounded() -> None:
    for seconds in (0, 3_601):
        with pytest.raises(ValidationError):
            TelemetrySettings(otlp_metrics_export_interval_seconds=seconds)


def test_runtime_metrics_reach_an_otlp_collector() -> None:
    received: list[tuple[str, str, bytes]] = []

    class Collector(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers["Content-Type"], body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = otlp_meter_provider(
        f"http://127.0.0.1:{server.server_port}/v1/metrics", export_interval_millis=60_000
    )
    try:
        counter = provider.get_meter("algen_agent_runtime.cache").create_counter(
            "algen_agent_runtime.cache.operations"
        )
        counter.add(3, {"cache.operation": "get"})
        assert provider.force_flush(timeout_millis=10_000)
    finally:
        provider.shutdown()
        server.shutdown()
        server.server_close()

    # The flush exports; shutting down may export once more.
    assert received
    assert {(path, content_type) for path, content_type, _ in received} == {
        ("/v1/metrics", "application/x-protobuf")
    }
    assert b"algen_agent_runtime.cache.operations" in received[0][2]


def test_a_second_configuration_keeps_the_runtime_provider_without_a_warning(
    installed: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The global getter returns whatever was installed last, as the real API does.
    monkeypatch.setattr(
        setup.metrics,
        "get_meter_provider",
        lambda: installed["meter"][-1] if installed["meter"] else NoOpMeterProvider(),
    )
    settings = TelemetrySettings(otlp_metrics_endpoint="http://127.0.0.1:4318/v1/metrics")

    with capture_logs() as logs:
        configure_telemetry(settings)
        configure_telemetry(settings)

    assert len(installed["meter"]) == 1
    assert not [log for log in logs if log["event"] == "otlp_metrics_endpoint_ignored"]


def test_flush_exports_only_the_provider_the_runtime_installed(
    installed: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[float] = []
    flush_telemetry()  # Nothing installed: nothing to flush, no error.

    configure_telemetry(TelemetrySettings(otlp_metrics_endpoint="http://127.0.0.1:4318/v1/metrics"))
    provider = installed["meter"][0]
    monkeypatch.setattr(
        provider, "force_flush", lambda timeout_millis: flushed.append(timeout_millis)
    )
    flush_telemetry(timeout_millis=500)

    assert flushed == [500]
