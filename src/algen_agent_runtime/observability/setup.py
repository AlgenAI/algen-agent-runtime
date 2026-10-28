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
        current = metrics.get_meter_provider()
        if isinstance(current, MeterProvider):
            # Readers are fixed when a provider is built: one installed first (by Traccia with
            # its metrics on, or by the application) keeps exporting where it was told to.
            _logger.warning(
                "otlp_metrics_endpoint_ignored",
                reason="an OpenTelemetry SDK meter provider is already installed",
            )
        else:
            metrics.set_meter_provider(
                otlp_meter_provider(
                    settings.otlp_metrics_endpoint,
                    export_interval_millis=settings.otlp_metrics_export_interval_seconds * 1000,
                )
            )
    return trace.get_tracer(settings.service_name), metrics.get_meter(settings.service_name)
