from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.analytics import AnalyticalResult, GraphExecutionContext
from algen_agent_runtime.semantics import AnalysisKind, AnalysisOutcome


class MethodImplementationKind(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL_SERVICE = "model_service"


class MethodLifecycle(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class AnalyticalMethodManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str
    description: str
    analysis_kinds: tuple[AnalysisKind, ...] = Field(min_length=1)
    implementation_kind: MethodImplementationKind = MethodImplementationKind.DETERMINISTIC
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "array"})
    parameter_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    required_metrics: tuple[str, ...] = ()
    required_dimensions: tuple[str, ...] = ()
    required_history_points: int = Field(default=0, ge=0)
    minimum_sample_size: int = Field(default=0, ge=0)
    required_model_artifacts: tuple[str, ...] = ()
    emits_uncertainty: bool = False
    approval_required: bool = False
    lifecycle: MethodLifecycle = MethodLifecycle.DRAFT
    validation_reference: str | None = None
    backtest_reference: str | None = None
    deprecation_message: str | None = None
    remote_model_service: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_implementation(self) -> AnalyticalMethodManifest:
        if (
            self.implementation_kind == MethodImplementationKind.MODEL_SERVICE
            and not self.remote_model_service
        ):
            raise ValueError("model-service methods require remote_model_service")
        if self.lifecycle == MethodLifecycle.DEPRECATED and not self.deprecation_message:
            raise ValueError("deprecated methods require deprecation_message")
        return self


class MethodExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    method: str
    version: str | None = None
    inputs: tuple[AnalyticalResult, ...]
    parameters: dict[str, Any] = Field(default_factory=dict)
    available_model_artifacts: tuple[str, ...] = ()


class AnalyticalMethod(Protocol):
    async def __call__(
        self,
        inputs: tuple[AnalyticalResult, ...],
        parameters: dict[str, Any],
        context: GraphExecutionContext,
    ) -> AnalysisOutcome: ...
