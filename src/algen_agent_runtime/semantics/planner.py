from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.analytics.contracts import (
    AnalyticalGraph,
    AnalyticalNode,
    AnalyticalNodeKind,
    NodeBudget,
    NodeCachePolicy,
    NodeRetryPolicy,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.semantics.contracts import MetricQuery
from algen_agent_runtime.semantics.layer import SemanticLayer
from algen_agent_runtime.semantics.production_compiler import ProductionSemanticQueryCompiler


class SemanticQueryTask(BaseModel):
    """One independently executable semantic result set in an analysis graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    query: MetricQuery
    output_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "array", "items": {"type": "object"}}
    )
    retry: NodeRetryPolicy = Field(default_factory=NodeRetryPolicy)
    cache: NodeCachePolicy = Field(default_factory=NodeCachePolicy)
    budget: NodeBudget = Field(default_factory=NodeBudget)
    concurrency_key: str | None = None


class SemanticAnalysisPlan(BaseModel):
    """Declarative semantic queries plus explicit downstream transformations."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    version: str
    queries: tuple[SemanticQueryTask, ...] = Field(min_length=1)
    transformations: tuple[AnalyticalNode, ...] = ()
    maximum_concurrency: int = Field(default=4, ge=1, le=128)
    maximum_nodes: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: float = Field(default=300, gt=0, le=86_400)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_identifiers(self) -> SemanticAnalysisPlan:
        query_ids = [item.id for item in self.queries]
        transformation_ids = [item.id for item in self.transformations]
        all_ids = query_ids + transformation_ids
        if len(all_ids) != len(set(all_ids)):
            raise ValueError("semantic plan node identifiers must be unique")
        return self


class SemanticQueryGraphPlanner:
    """Lowers governed semantic tasks into a validated analytical execution DAG."""

    def __init__(self, compiler: ProductionSemanticQueryCompiler) -> None:
        self._compiler = compiler

    def plan(self, layer: SemanticLayer, definition: SemanticAnalysisPlan) -> AnalyticalGraph:
        query_nodes = tuple(
            AnalyticalNode(
                id=task.id,
                kind=AnalyticalNodeKind.SEMANTIC_QUERY,
                configuration={
                    "compiled_query": self._compiler.compile(layer, task.query).model_dump(
                        mode="json"
                    )
                },
                output_schema=task.output_schema,
                retry=task.retry,
                cache=task.cache,
                budget=task.budget,
                concurrency_key=task.concurrency_key,
            )
            for task in definition.queries
        )
        try:
            return AnalyticalGraph(
                name=definition.name,
                version=definition.version,
                nodes=query_nodes + definition.transformations,
                maximum_concurrency=definition.maximum_concurrency,
                maximum_nodes=definition.maximum_nodes,
                timeout_seconds=definition.timeout_seconds,
                metadata={
                    **definition.metadata,
                    "semantic_layer": layer.definition.name,
                    "semantic_layer_version": layer.definition.version,
                    "semantic_layer_digest": layer.digest,
                },
            )
        except ValueError as exc:
            raise ConfigurationError(f"invalid semantic analysis plan: {exc}") from exc
