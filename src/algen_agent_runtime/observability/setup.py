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


def configure_telemetry(settings: TelemetrySettings) -> tuple[Any, Any]:
    # Avoid claiming OpenTelemetry's process-global provider when there is no
    # exporter. An application-level Traccia adapter may initialize it later.
    if settings.enabled and settings.otlp_endpoint:
        tracer_provider = trace.get_tracer_provider()
        if not isinstance(tracer_provider, TracerProvider):
            tracer_provider = TracerProvider()
            trace.set_tracer_provider(tracer_provider)
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint))
        )
        if not isinstance(metrics.get_meter_provider(), MeterProvider):
            metrics.set_meter_provider(MeterProvider())
    return trace.get_tracer(settings.service_name), metrics.get_meter(settings.service_name)
