from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, cast

import jsonschema
from opentelemetry import metrics, trace

from algen_agent_runtime.analytics import AnalyticalResult, GraphExecutionContext
from algen_agent_runtime.exceptions.errors import ConfigurationError, NotFoundError
from algen_agent_runtime.methods.contracts import (
    AnalyticalMethod,
    AnalyticalMethodManifest,
    MethodExecutionRequest,
    MethodLifecycle,
)
from algen_agent_runtime.semantics import AnalysisOutcome


class AnalyticalMethodRegistry:
    """Versioned registry for domain-supplied deterministic or model-backed methods."""

    def __init__(self) -> None:
        self._methods: dict[tuple[str, str], tuple[AnalyticalMethodManifest, AnalyticalMethod]] = {}
        self._tracer = trace.get_tracer("algen_agent_runtime.methods")
        self._counter = metrics.get_meter("algen_agent_runtime.methods").create_counter(
            "algen_agent_runtime.method.executions"
        )

    def register(
        self, manifest: AnalyticalMethodManifest, implementation: AnalyticalMethod
    ) -> None:
        key = (manifest.name, manifest.version)
        if key in self._methods:
            raise ConfigurationError(f"analytical method {manifest.name}@{manifest.version} exists")
        self._methods[key] = (manifest, implementation)

    def discover(self, *, include_deprecated: bool = False) -> tuple[AnalyticalMethodManifest, ...]:
        return tuple(
            manifest
            for manifest, _ in sorted(
                self._methods.values(), key=lambda item: (item[0].name, item[0].version)
            )
            if include_deprecated or manifest.lifecycle != MethodLifecycle.DEPRECATED
        )

    def resolve(
        self, name: str, version: str | None = None
    ) -> tuple[AnalyticalMethodManifest, AnalyticalMethod]:
        candidates = [item for key, item in self._methods.items() if key[0] == name]
        if version is not None:
            candidates = [item for item in candidates if item[0].version == version]
        candidates = [
            item for item in candidates if item[0].lifecycle != MethodLifecycle.DEPRECATED
        ]
        if not candidates:
            raise NotFoundError(
                f"analytical method {name!r} version {version or 'latest'!r} not found"
            )
        return sorted(candidates, key=lambda item: item[0].version)[-1]

    async def execute(
        self, request: MethodExecutionRequest, context: GraphExecutionContext
    ) -> AnalysisOutcome:
        manifest, implementation = self.resolve(request.method, request.version)
        values: Any = [item.value for item in request.inputs]
        jsonschema.validate(values, manifest.input_schema)
        jsonschema.validate(request.parameters, manifest.parameter_schema)
        if sum(item.row_count or 0 for item in request.inputs) < manifest.minimum_sample_size:
            raise ConfigurationError(
                f"method {manifest.name!r} requires at least {manifest.minimum_sample_size} rows"
            )
        available_metrics = {
            metric for item in request.inputs for metric in item.provenance.metric_versions
        }
        missing_metrics = set(manifest.required_metrics) - available_metrics
        if missing_metrics:
            raise ConfigurationError(
                f"method {manifest.name!r} requires metrics {sorted(missing_metrics)}"
            )
        available_dimensions = {
            column for item in request.inputs for column in item.provenance.source_columns
        }
        missing_dimensions = set(manifest.required_dimensions) - available_dimensions
        if missing_dimensions:
            raise ConfigurationError(
                f"method {manifest.name!r} requires dimensions {sorted(missing_dimensions)}"
            )
        if (
            manifest.required_history_points
            and sum(item.row_count or 0 for item in request.inputs)
            < manifest.required_history_points
        ):
            raise ConfigurationError(
                f"method {manifest.name!r} requires {manifest.required_history_points} history points"
            )
        approved_methods = set(context.attributes.get("approved_methods", ()))
        if manifest.approval_required and manifest.name not in approved_methods:
            raise ConfigurationError(f"method {manifest.name!r} requires approval")
        missing_artifacts = set(manifest.required_model_artifacts) - set(
            request.available_model_artifacts
        )
        if missing_artifacts:
            raise ConfigurationError(
                f"method {manifest.name!r} requires model artifacts {sorted(missing_artifacts)}"
            )
        attributes = {
            "analytics.method.name": manifest.name,
            "analytics.method.version": manifest.version,
            "analytics.method.kind": manifest.implementation_kind.value,
        }
        with self._tracer.start_as_current_span("analytical_method.execute", attributes=attributes):
            invoked: Any = implementation(request.inputs, request.parameters, context)
            if inspect.isawaitable(invoked):
                invoked = await invoked
            validated = AnalysisOutcome.model_validate(cast(Any, invoked))
            if manifest.output_schema:
                jsonschema.validate(validated.model_dump(mode="json"), manifest.output_schema)
            self._counter.add(1, {**attributes, "outcome": validated.status.value})
            return validated


def adapt_sync_method(
    function: Callable[[list[dict[str, Any]], dict[str, Any]], AnalysisOutcome],
) -> AnalyticalMethod:
    async def adapted(
        inputs: tuple[AnalyticalResult, ...],
        parameters: dict[str, Any],
        context: GraphExecutionContext,
    ) -> AnalysisOutcome:
        del context
        rows: list[dict[str, Any]] = []
        for result in inputs:
            if isinstance(result.value, list):
                rows.extend(item for item in result.value if isinstance(item, dict))
        return function(rows, parameters)

    return adapted
