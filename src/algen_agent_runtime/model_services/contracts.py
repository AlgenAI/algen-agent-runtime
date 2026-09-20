from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class ModelServiceKind(StrEnum):
    FORECAST = "forecast"
    OPTIMIZATION = "optimization"
    SIMULATION = "simulation"
    CAUSAL = "causal"


class DriftState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    WARNING = "warning"
    DRIFTED = "drifted"


class ModelServiceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str
    kind: ModelServiceKind
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    feature_lineage: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    training_reference: str | None = None
    validation_reference: str | None = None
    maximum_batch_size: int = Field(default=100, ge=1, le=10_000)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    drift_state: DriftState = DriftState.UNKNOWN
    confidence_supported: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelServiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    model: str
    version: str | None = None
    inputs: tuple[dict[str, Any], ...] = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)


class ModelServiceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    model: str
    version: str
    outputs: tuple[dict[str, Any], ...]
    confidence_intervals: tuple[tuple[float, float] | None, ...] = ()
    feature_lineage: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelServiceHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    healthy: bool
    drift_state: DriftState
    message: str | None = None


class ModelService(Protocol):
    manifest: ModelServiceManifest

    async def predict(self, request: ModelServiceRequest) -> ModelServiceResponse: ...
    async def health(self) -> ModelServiceHealth: ...
