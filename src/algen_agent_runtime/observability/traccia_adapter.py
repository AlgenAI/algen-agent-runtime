from __future__ import annotations

import importlib
import inspect
import os
from collections.abc import Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from types import MethodType, ModuleType
from typing import Any, Protocol, cast

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan, Status, StatusCode

from algen_agent_runtime.config.settings import TelemetrySettings, TracciaSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.observability.run_export import (
    RunScopedExportProcessor,
    install_run_scoped_export,
)
from algen_agent_runtime.observability.runtime_traces import (
    RUNTIME_ROOT_ATTRIBUTE,
    RuntimeTraceFilterProcessor,
    in_runtime_trace,
    install_runtime_trace_filter,
    runtime_trace_context,
)
from algen_agent_runtime.observability.trace_levels import TraceLevel


class _DeferredGuardrailSpan(NonRecordingSpan):
    """Collect guardrail attributes so compact traces can retain only triggers."""

    def __init__(self) -> None:
        super().__init__(INVALID_SPAN_CONTEXT)
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class ObservabilityAdapter(Protocol):
    def start(self) -> None: ...

    def tracer(self, name: str) -> Any:
        """The tracer Runtime components create their spans with, so every span takes the
        adapter's path (for Traccia, its enrichment processors)."""
        ...

    def run_scope(
        self, *, agent_id: str, agent_name: str, tenant_id: str
    ) -> AbstractContextManager[Any]: ...

    def span_attributes(self) -> Mapping[str, str]: ...

    def span_scope(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any],
        root: bool = False,
    ) -> AbstractContextManager[Any] | None: ...

    def set_span_outcome(
        self, span: Any, *, failed: bool, description: str | None = None
    ) -> None: ...

    def guardrail_scope(
        self,
        *,
        name: str,
        category: str,
        enforcement_mode: str,
        policy_id: str | None = None,
    ) -> AbstractContextManager[Any]: ...

    def govern(self, invocation: Any, *, agent_id: str, agent_name: str) -> Any: ...

    def stop(self) -> None: ...


class NoopObservabilityAdapter:
    def __init__(self, trace_level: TraceLevel = "detailed") -> None:
        self._trace_level = trace_level

    def start(self) -> None:
        return None

    def tracer(self, name: str) -> Any:
        return trace.get_tracer(name)

    def run_scope(
        self, *, agent_id: str, agent_name: str, tenant_id: str
    ) -> AbstractContextManager[Any]:
        del agent_id, agent_name, tenant_id
        return nullcontext()

    def span_attributes(self) -> Mapping[str, str]:
        return {}

    def span_scope(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any],
        root: bool = False,
    ) -> AbstractContextManager[Any] | None:
        del name, attributes, root
        return None

    def set_span_outcome(self, span: Any, *, failed: bool, description: str | None = None) -> None:
        span.set_status(
            Status(
                StatusCode.ERROR if failed else StatusCode.OK,
                description if failed else None,
            )
        )

    def guardrail_scope(
        self,
        *,
        name: str,
        category: str,
        enforcement_mode: str,
        policy_id: str | None = None,
    ) -> AbstractContextManager[Any]:
        attributes: dict[str, Any] = {
            "span.type": "guardrail",
            "guardrail.name": name,
            "guardrail.category": category,
            "guardrail.enforcement_mode": enforcement_mode,
            "guardrail.source_sdk": "algen_agent_runtime",
            "guardrail.evidence_type": "span_attribute",
        }
        if policy_id:
            attributes["guardrail.policy_id"] = policy_id
        tracer = self.tracer("algen_agent_runtime.guardrails")
        if self._trace_level == "detailed":
            return cast(
                AbstractContextManager[Any],
                tracer.start_as_current_span(f"guardrail.{name}", attributes=attributes),
            )

        @contextmanager
        def triggered_only_scope() -> Any:
            deferred = _DeferredGuardrailSpan()
            yield deferred
            if deferred.attributes.get("guardrail.triggered") is not True:
                return
            with tracer.start_as_current_span(f"guardrail.{name}", attributes=attributes) as span:
                for key, value in deferred.attributes.items():
                    span.set_attribute(key, value)

        return triggered_only_scope()

    def govern(self, invocation: Any, *, agent_id: str, agent_name: str) -> Any:
        del agent_id, agent_name
        return invocation

    def stop(self) -> None:
        return None


class _TracciaTracer:
    """Creates spans through Traccia's tracer, which runs its enrichment processors (agent
    identity, tokens, cost, guardrail detection, governance) on every span. Traccia starts on the
    first span, not when the container is built."""

    def __init__(self, adapter: TracciaObservabilityAdapter, name: str) -> None:
        self._adapter = adapter
        self._name = name

    def start_as_current_span(
        self, name: str, attributes: Mapping[str, Any] | None = None, **_: Any
    ) -> AbstractContextManager[Any]:
        self._adapter.start()
        module = self._adapter._module
        assert module is not None
        return cast(
            AbstractContextManager[Any],
            module.get_tracer(self._name).start_as_current_span(
                name, attributes=dict(attributes or {})
            ),
        )


class _RunSpanCountEnricher:
    """A Traccia enrichment processor: writes ``agent.run.span_count`` onto each run's root while
    it can still change, from what the run-scoped export counted."""

    def __init__(self, export: RunScopedExportProcessor) -> None:
        self._export = export

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        return None

    def on_end(self, span: Any) -> None:
        if getattr(span, "name", None) != "agent.run":
            return
        otel_span = getattr(span, "_otel_span", span)
        context = otel_span.get_span_context()
        count = self._export.span_count(context.trace_id, context.span_id)
        if count is not None:
            span.set_attribute("agent.run.span_count", count)

    def shutdown(self) -> None:
        return None

    def force_flush(self, timeout: float | None = None) -> bool:
        return True


class TracciaObservabilityAdapter:
    """Optional Traccia lifecycle and per-run identity bridge."""

    def __init__(
        self,
        settings: TracciaSettings,
        service_name: str,
        trace_level: TraceLevel = "detailed",
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._service_name = service_name
        self._trace_level = trace_level
        self._environment = dict(environment or {})
        self._module: ModuleType | Any | None = None
        self._started = False
        self._run_export: RunScopedExportProcessor | None = None
        self._trace_filter: RuntimeTraceFilterProcessor | None = None

    def tracer(self, name: str) -> Any:
        return _TracciaTracer(self, name)

    def start(self) -> None:
        if self._started:
            return
        try:
            module = importlib.import_module("traccia")
        except ModuleNotFoundError as exc:
            raise ConfigurationError(
                "Traccia observability is enabled but the SDK is not installed; "
                "install algen-agent-runtime[traccia]"
            ) from exc
        options: dict[str, Any] = {
            "service_name": self._service_name,
            "service_role": self._settings.service_role,
            "env": self._settings.environment,
            "auto_start_trace": False,
            "sample_rate": self._settings.sample_rate,
            "enable_patching": self._settings.enable_patching,
            "enable_token_counting": self._settings.enable_token_counting,
            "enable_costs": self._settings.enable_costs,
            "enable_metrics": self._settings.enable_metrics,
            "redact_pii": self._settings.redact_pii,
        }
        if self._settings.endpoint:
            options["endpoint"] = self._settings.endpoint
        if self._settings.metrics_endpoint:
            options["metrics_endpoint"] = self._settings.metrics_endpoint
        if self._settings.max_spans_per_second is not None:
            options["max_spans_per_second"] = self._settings.max_spans_per_second
        if self._settings.api_key:
            variable = self._settings.api_key.removeprefix("env://")
            value = self._environment.get(variable) or os.getenv(variable)
            if not value:
                raise ConfigurationError(
                    f"required Traccia secret environment variable {variable!r} is not set"
                )
            options["api_key"] = value
        provider = module.init(**options)
        self._ensure_span_processor_compatibility(provider)
        self._register_otel_provider(provider)
        otel_provider = getattr(provider, "_otel_tracer_provider", None)
        if self._settings.export_whole_runs and self._run_export is None and otel_provider:
            self._run_export = install_run_scoped_export(
                otel_provider,
                max_spans=self._settings.run_export_max_spans,
                max_seconds=self._settings.run_export_max_seconds,
            )
            if self._run_export is not None:
                provider.add_span_processor(_RunSpanCountEnricher(self._run_export))
        # Installed after the run-scoped export so it sits in front of it and decides first.
        if (
            not self._settings.export_foreign_traces
            and self._trace_filter is None
            and otel_provider
        ):
            self._trace_filter = install_runtime_trace_filter(otel_provider)
        self._module = module
        self._started = True

    @staticmethod
    def _ensure_span_processor_compatibility(provider: Any) -> None:
        """Bridge Traccia processors to OpenTelemetry's mutable-span hook.

        OpenTelemetry 1.43 added ``SpanProcessor._on_ending`` before ``on_end``. Traccia 0.1.29
        processors implement the older public lifecycle only. Adding a bounded compatibility hook
        prevents otherwise valid agent runs from failing while retaining downstream processor hooks.
        """
        otel_provider = getattr(provider, "_otel_tracer_provider", None)
        active = getattr(otel_provider, "_active_span_processor", None)
        processors = getattr(active, "_span_processors", ())

        def on_ending(processor: Any, span: Any) -> None:
            downstream = getattr(processor, "next_processor", None)
            callback = getattr(downstream, "_on_ending", None)
            if callback is not None:
                callback(span)

        for processor in processors:
            if not hasattr(processor, "_on_ending"):
                processor._on_ending = MethodType(on_ending, processor)

    @staticmethod
    def _register_otel_provider(provider: Any) -> None:
        otel_provider = getattr(provider, "_otel_tracer_provider", None)
        if not isinstance(otel_provider, TracerProvider):
            raise ConfigurationError(
                "installed Traccia SDK does not expose a compatible OpenTelemetry provider"
            )
        current = trace.get_tracer_provider()
        if isinstance(current, trace.ProxyTracerProvider):
            trace.set_tracer_provider(otel_provider)
        elif current is not otel_provider:
            raise ConfigurationError(
                "Traccia must be initialized before another global OpenTelemetry provider"
            )

    def run_scope(
        self, *, agent_id: str, agent_name: str, tenant_id: str
    ) -> AbstractContextManager[Any]:
        self.start()
        assert self._module is not None
        identity = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "env": self._settings.environment,
            "tenant_id": tenant_id,
        }
        if self._settings.project_id:
            identity["project_id"] = self._settings.project_id
        return cast(
            AbstractContextManager[Any],
            self._module.runtime_config.run_identity(**identity),
        )

    def span_attributes(self) -> Mapping[str, str]:
        return {
            "env": self._settings.environment,
            "environment": self._settings.environment,
        }

    def span_scope(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any],
        root: bool = False,
    ) -> AbstractContextManager[Any] | None:
        """Create a Traccia-managed span so enrichment processors see its end.

        Raw OpenTelemetry children still inherit this span. A managed root is
        required for Traccia's guardrail detector to attach the trace-level
        ``guardrail.summary`` consumed by Guardrail Posture.

        A span that opens a Runtime trace in this process carries ``algen.runtime.root``. When
        foreign traces are not exported, a span that would otherwise join a live span the Runtime
        did not start (an HTTP request's, say) opens its own trace instead, so it is not dropped
        with that request.
        """
        self.start()
        module = self._module
        assert module is not None

        @contextmanager
        def scope() -> Any:
            current = trace.get_current_span()
            opens_trace = root or (
                not self._settings.export_foreign_traces
                and current.is_recording()
                and not in_runtime_trace()
            )
            span_attributes = dict(attributes)
            # No live local parent: the first span, or a resumed run under its stored parent.
            if opens_trace or not current.is_recording():
                span_attributes[RUNTIME_ROOT_ATTRIBUTE] = True
            token = otel_context.attach(
                runtime_trace_context(Context() if opens_trace else otel_context.get_current())
            )
            try:
                with module.span(name, attributes=span_attributes) as span:
                    yield span
            finally:
                otel_context.detach(token)

        return scope()

    def set_span_outcome(self, span: Any, *, failed: bool, description: str | None = None) -> None:
        status_type = type(span.status)
        status = status_type.ERROR if failed else status_type.OK
        span.set_status(status, description if failed else None)

    def guardrail_scope(
        self,
        *,
        name: str,
        category: str,
        enforcement_mode: str,
        policy_id: str | None = None,
    ) -> AbstractContextManager[Any]:
        """Create an explicit Tier-A guardrail span through the Traccia SDK.

        The SDK's guardrail processor detects these child spans and writes the
        aggregate ``guardrail.summary`` onto the root conversation trace.
        """
        self.start()
        guardrails = importlib.import_module("traccia.guardrails")
        if self._trace_level == "detailed":
            return cast(
                AbstractContextManager[Any],
                guardrails.guardrail_span(
                    name,
                    category=category,
                    enforcement_mode=enforcement_mode,
                    policy_id=policy_id,
                ),
            )

        @contextmanager
        def triggered_only_scope() -> Any:
            deferred = _DeferredGuardrailSpan()
            yield deferred
            if deferred.attributes.get("guardrail.triggered") is not True:
                return
            with guardrails.guardrail_span(
                name,
                category=category,
                enforcement_mode=enforcement_mode,
                policy_id=policy_id,
            ) as span:
                for key, value in deferred.attributes.items():
                    span.set_attribute(key, value)

        return triggered_only_scope()

    def govern(self, invocation: Any, *, agent_id: str, agent_name: str) -> Any:
        if not self._settings.governance_enabled:
            return invocation
        governed: Any | None = None

        async def async_invocation(*args: Any, **kwargs: Any) -> Any:
            nonlocal governed
            self.start()
            assert self._module is not None
            if governed is None:
                governed = self._module.govern(
                    agent_id=self._settings.governance_agent_id or agent_id,
                    fail_open=self._settings.governance_fail_open,
                    name=f"{agent_name}.governed_run",
                    attributes={"agent.name": agent_name},
                )(invocation)
            result = governed(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result

        return async_invocation

    def stop(self) -> None:
        if not self._started or self._module is None:
            return
        # OpenTelemetry providers are process-global and cannot be replaced.
        # Flush per-container work; the SDK's process-exit hook owns shutdown.
        force_flush = getattr(self._module, "force_flush", None)
        if force_flush is not None:
            force_flush(self._settings.flush_timeout_seconds)
        self._started = False


def observability_adapter(
    settings: TelemetrySettings,
    environment: Mapping[str, str] | None = None,
) -> ObservabilityAdapter:
    if settings.enabled and settings.traccia.enabled:
        return TracciaObservabilityAdapter(
            settings.traccia,
            settings.service_name,
            settings.trace_level,
            environment,
        )
    return NoopObservabilityAdapter(settings.trace_level)
