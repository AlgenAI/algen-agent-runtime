from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import jsonschema
from opentelemetry import metrics, trace

from algen_agent_runtime.exceptions.errors import ConfigurationError, NotFoundError, ProviderError
from algen_agent_runtime.model_services.contracts import (
    DriftState,
    ModelService,
    ModelServiceHealth,
    ModelServiceKind,
    ModelServiceManifest,
    ModelServiceRequest,
    ModelServiceResponse,
)


class ModelServiceRegistry:
    """Provider-neutral predictive service router; intentionally separate from LLMs."""

    def __init__(self) -> None:
        self._services: dict[tuple[str, str], ModelService] = {}
        self._tracer = trace.get_tracer("algen_agent_runtime.model_services")
        self._counter = metrics.get_meter("algen_agent_runtime.model_services").create_counter(
            "algen_agent_runtime.model_service.calls"
        )

    def register(self, service: ModelService) -> None:
        key = (service.manifest.name, service.manifest.version)
        if key in self._services:
            raise ConfigurationError(f"model service {key[0]}@{key[1]} already exists")
        self._services[key] = service

    def discover(self, kind: ModelServiceKind | None = None) -> tuple[ModelServiceManifest, ...]:
        return tuple(
            service.manifest
            for _, service in sorted(self._services.items())
            if service.manifest.enabled and (kind is None or service.manifest.kind == kind)
        )

    async def invoke(
        self,
        request: ModelServiceRequest,
        *,
        fallback: tuple[tuple[str, str | None], ...] = (),
        allow_drifted: bool = False,
    ) -> ModelServiceResponse:
        candidates = ((request.model, request.version), *fallback)
        errors: list[str] = []
        for name, version in candidates:
            try:
                service = self._resolve(name, version)
                manifest = service.manifest
                if manifest.drift_state == DriftState.DRIFTED and not allow_drifted:
                    raise ProviderError("model is marked drifted")
                health = await service.health()
                if not health.healthy:
                    raise ProviderError(health.message or "model service is unhealthy")
                if len(request.inputs) > manifest.maximum_batch_size:
                    raise ConfigurationError(
                        f"model service {name!r} maximum batch size is {manifest.maximum_batch_size}"
                    )
                for item in request.inputs:
                    jsonschema.validate(item, manifest.input_schema)
                effective = request.model_copy(update={"model": name, "version": manifest.version})
                timeout = request.timeout_seconds or manifest.timeout_seconds
                attributes = {
                    "model_service.name": name,
                    "model_service.version": manifest.version,
                    "model_service.kind": manifest.kind.value,
                }
                with self._tracer.start_as_current_span(
                    "model_service.invoke", attributes=attributes
                ):
                    async with asyncio.timeout(timeout):
                        response = await service.predict(effective)
                    for item in response.outputs:
                        jsonschema.validate(item, manifest.output_schema)
                    if len(response.outputs) != len(request.inputs):
                        raise ProviderError("model service returned a mismatched batch size")
                    self._counter.add(1, {**attributes, "outcome": "completed"})
                    return response
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors.append(f"{name}@{version or 'latest'}: {exc}")
        raise ProviderError("all model services failed: " + "; ".join(errors), retryable=True)

    def _resolve(self, name: str, version: str | None) -> ModelService:
        candidates = [
            service
            for (candidate_name, candidate_version), service in self._services.items()
            if candidate_name == name
            and (version is None or candidate_version == version)
            and service.manifest.enabled
        ]
        if not candidates:
            raise NotFoundError(f"model service {name}@{version or 'latest'} not found")
        return sorted(candidates, key=lambda item: item.manifest.version)[-1]


class DeterministicModelService:
    def __init__(
        self,
        manifest: ModelServiceManifest,
        function: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    ) -> None:
        self.manifest = manifest
        self._function = function

    async def predict(self, request: ModelServiceRequest) -> ModelServiceResponse:
        outputs = tuple(self._function(item, request.parameters) for item in request.inputs)
        return ModelServiceResponse(
            request_id=request.id,
            model=self.manifest.name,
            version=self.manifest.version,
            outputs=outputs,
            feature_lineage=self.manifest.feature_lineage,
        )

    async def health(self) -> ModelServiceHealth:
        return ModelServiceHealth(healthy=True, drift_state=self.manifest.drift_state)
