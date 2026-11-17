"""Experimental: remote (non-LLM) model services, their registry, health and drift.

Outside the compatibility promise (``docs/api-stability.md``, "Experimental interfaces"): no
application uses them in production yet, and the HTTP client is tested against local fakes only.
"""

from algen_agent_runtime.model_services.contracts import (
    DriftState,
    ModelServiceHealth,
    ModelServiceKind,
    ModelServiceManifest,
    ModelServiceRequest,
    ModelServiceResponse,
)
from algen_agent_runtime.model_services.http import HTTPModelService
from algen_agent_runtime.model_services.registry import (
    DeterministicModelService,
    ModelServiceRegistry,
)

#: Marks this package experimental (``algen_agent_runtime.stability``).
__experimental__ = (
    "no application uses remote model services in production yet; the HTTP client, health and "
    "drift contracts are tested against local fakes only"
)

__all__ = [
    "DeterministicModelService",
    "DriftState",
    "HTTPModelService",
    "ModelServiceHealth",
    "ModelServiceKind",
    "ModelServiceManifest",
    "ModelServiceRegistry",
    "ModelServiceRequest",
    "ModelServiceResponse",
]
