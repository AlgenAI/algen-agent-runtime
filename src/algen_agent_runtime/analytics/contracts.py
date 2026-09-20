from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.types.contracts import utc_now


class AnalyticalNodeKind(StrEnum):
    SEMANTIC_QUERY = "semantic_query"
    JOIN_RESULTS = "join_results"
    COMPARE_PERIODS = "compare_periods"
    CALCULATE = "calculate"
    MODEL_CALL = "model_call"
    RANK = "rank"
    VERIFY = "verify"
    COMPOSE = "compose"


class NodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class GraphStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeRetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    maximum_attempts: int = Field(default=1, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=0.1, ge=0, le=60)
    maximum_backoff_seconds: float = Field(default=5, ge=0, le=300)


class NodeCachePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    policy_name: str = "analytical_nodes"


class NodeBudget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timeout_seconds: float = Field(default=30, gt=0, le=3600)
    maximum_rows: int = Field(default=10_000, ge=1, le=1_000_000)
    maximum_bytes: int = Field(default=10_000_000, ge=1)
    maximum_cost_usd: float | None = Field(default=None, ge=0)


class ResultReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str
    path: str | None = None


class AnalyticalNode(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    kind: AnalyticalNodeKind
    dependencies: tuple[str, ...] = ()
    inputs: dict[str, ResultReference] = Field(default_factory=dict)
    configuration: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = Field(default_factory=dict)
    retry: NodeRetryPolicy = Field(default_factory=NodeRetryPolicy)
    cache: NodeCachePolicy = Field(default_factory=NodeCachePolicy)
    budget: NodeBudget = Field(default_factory=NodeBudget)
    concurrency_key: str | None = None

    @model_validator(mode="after")
    def validate_references(self) -> AnalyticalNode:
        referenced = {value.node_id for value in self.inputs.values()}
        if not referenced.issubset(set(self.dependencies)):
            raise ValueError("every input result reference must be declared as a dependency")
        if self.id in self.dependencies:
            raise ValueError("a node cannot depend on itself")
        return self


class AnalyticalGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    version: str
    nodes: tuple[AnalyticalNode, ...] = Field(min_length=1)
    maximum_concurrency: int = Field(default=4, ge=1, le=128)
    maximum_nodes: int = Field(default=100, ge=1, le=1000)
    timeout_seconds: float = Field(default=300, gt=0, le=86_400)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dag(self) -> AnalyticalGraph:
        if len(self.nodes) > self.maximum_nodes:
            raise ValueError("graph exceeds maximum_nodes")
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("graph node identifiers must be unique")
        known = set(ids)
        for node in self.nodes:
            missing = set(node.dependencies) - known
            if missing:
                raise ValueError(f"node {node.id!r} has unknown dependencies {sorted(missing)}")
        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {node.id: node.dependencies for node in self.nodes}

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("analytical graph contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in dependencies[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in ids:
            visit(node_id)
        return self


class ResultProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_ids: tuple[str, ...] = ()
    source_columns: tuple[str, ...] = ()
    upstream_result_ids: tuple[str, ...] = ()
    semantic_layer_digest: str | None = None
    metric_versions: dict[str, str] = Field(default_factory=dict)
    method_version: str | None = None
    model_version: str | None = None
    query_fingerprint: str | None = None
    freshness_observed_at: datetime | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)


class AnalyticalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(default_factory=lambda: str(uuid4()))
    node_id: str
    value: Any
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    row_count: int | None = Field(default=None, ge=0)
    byte_count: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    provenance: ResultProvenance = Field(default_factory=ResultProvenance)
    warnings: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class NodeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    result_id: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AnalyticalGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    tenant_id: str
    user_id: str
    graph: AnalyticalGraph
    status: GraphStatus = GraphStatus.PENDING
    nodes: dict[str, NodeExecution] = Field(default_factory=dict)
    results: dict[str, AnalyticalResult] = Field(default_factory=dict)
    version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GraphExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tenant_id: str
    user_id: str
    session_id: str | None = None
    run_id: str | None = None
    authorization_fingerprint: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class AnalyticalNodeHandler(Protocol):
    async def __call__(
        self,
        node: AnalyticalNode,
        inputs: dict[str, Any],
        context: GraphExecutionContext,
    ) -> AnalyticalResult: ...


class AnalyticalGraphStore(Protocol):
    async def create(self, state: AnalyticalGraphState) -> None: ...
    async def get(self, graph_id: str, tenant_id: str) -> AnalyticalGraphState | None: ...
    async def save(self, state: AnalyticalGraphState, expected_version: int) -> None: ...
