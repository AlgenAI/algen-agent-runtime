from __future__ import annotations

import httpx

from algen_agent_runtime.model_services.contracts import (
    ModelServiceHealth,
    ModelServiceManifest,
    ModelServiceRequest,
    ModelServiceResponse,
)
from algen_agent_runtime.security.network import validate_outbound_url


class HTTPModelService:
    """Remote model adapter with SSRF validation and no provider-specific SDK leakage."""

    def __init__(
        self,
        manifest: ModelServiceManifest,
        endpoint: str,
        *,
        headers: dict[str, str] | None = None,
        allowed_hosts: tuple[str, ...] = (),
        health_endpoint: str | None = None,
    ) -> None:
        self.manifest = manifest
        self._endpoint = endpoint
        self._headers = dict(headers or {})
        self._allowed_hosts = allowed_hosts
        self._health_endpoint = health_endpoint

    async def predict(self, request: ModelServiceRequest) -> ModelServiceResponse:
        await validate_outbound_url(self._endpoint, allowed_hosts=self._allowed_hosts)
        async with httpx.AsyncClient(
            timeout=request.timeout_seconds or self.manifest.timeout_seconds
        ) as client:
            response = await client.post(
                self._endpoint,
                headers=self._headers,
                json=request.model_dump(mode="json"),
            )
            response.raise_for_status()
            return ModelServiceResponse.model_validate(response.json())

    async def health(self) -> ModelServiceHealth:
        if self._health_endpoint is None:
            return ModelServiceHealth(
                healthy=True,
                drift_state=self.manifest.drift_state,
                message="No remote health endpoint configured; invocation health is unknown",
            )
        try:
            await validate_outbound_url(self._health_endpoint, allowed_hosts=self._allowed_hosts)
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(self._health_endpoint, headers=self._headers)
            return ModelServiceHealth(
                healthy=response.is_success,
                drift_state=self.manifest.drift_state,
                message=None
                if response.is_success
                else f"health returned HTTP {response.status_code}",
            )
        except Exception as exc:
            return ModelServiceHealth(
                healthy=False,
                drift_state=self.manifest.drift_state,
                message=f"health check failed: {type(exc).__name__}",
            )
