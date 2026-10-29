from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, cast

import structlog
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from algen_agent_runtime.config.settings import TelemetrySettings
from algen_agent_runtime.security.redaction import redact

_logger = structlog.get_logger("algen_agent_runtime.observability")

# The meter provider configure_telemetry installed, so a second call recognises its own provider
# and shutdown can flush it. Providers installed by Traccia or the application are never touched.
_installed_meter_provider: MeterProvider | None = None


def _redact_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    return cast(dict[str, Any], redact(event_dict))


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(stream=sys.stdout, level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _redact_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
    )


def otlp_meter_provider(endpoint: str, *, export_interval_millis: int) -> MeterProvider:
    """A meter provider that exports every instrument to an OTLP/HTTP metrics endpoint."""
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint), export_interval_millis=export_interval_millis
    )
    return MeterProvider(metric_readers=[reader])


def configure_telemetry(settings: TelemetrySettings) -> tuple[Any, Any]:
    # Avoid claiming OpenTelemetry's process-global providers when there is no
    # exporter. An application-level Traccia adapter may initialize them later.
    if settings.enabled and settings.otlp_endpoint:
        tracer_provider = trace.get_tracer_provider()
        if not isinstance(tracer_provider, TracerProvider):
            tracer_provider = TracerProvider()
            trace.set_tracer_provider(tracer_provider)
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
        )
    if settings.enabled and settings.otlp_metrics_endpoint:
        global _installed_meter_provider
        current = metrics.get_meter_provider()
        if current is _installed_meter_provider:
            # Called again in the same process (another lifespan): already exporting.
            return trace.get_tracer(settings.service_name), metrics.get_meter(settings.service_name)
        if isinstance(current, MeterProvider):
            # Readers are fixed when a provider is built: one installed first (by Traccia with
            # its metrics on, or by the application) keeps exporting where it was told to.
            _logger.warning(
                "otlp_metrics_endpoint_ignored",
                reason="an OpenTelemetry SDK meter provider is already installed",
            )
        else:
            provider = otlp_meter_provider(
                settings.otlp_metrics_endpoint,
                export_interval_millis=settings.otlp_metrics_export_interval_seconds * 1000,
            )
            metrics.set_meter_provider(provider)
            _installed_meter_provider = provider
    return trace.get_tracer(settings.service_name), metrics.get_meter(settings.service_name)


def flush_telemetry(timeout_millis: float = 10_000) -> None:
    """Export the metrics recorded since the last interval, if the Runtime installed the provider.

    Called on application shutdown, so the last interval is not left to the process's atexit hook,
    which embedded hosts and test runners may never reach. The provider stays installed: the
    process-global provider cannot be replaced, and a later lifespan in the same process reuses it.
    """
    if _installed_meter_provider is not None:
        _installed_meter_provider.force_flush(timeout_millis=timeout_millis)
