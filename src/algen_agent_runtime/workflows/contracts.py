from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.types.contracts import utc_now


class WorkflowNodeKind(StrEnum):
    AGENT = "agent"
    MAP_AGENT = "map_agent"
    HANDLER = "handler"
    PREDICATE = "predicate"
    JOIN = "join"


class WorkflowStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowNodeStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class WorkflowResourceKind(StrEnum):
    """Runtime resources that a workflow node is expected to use."""

    TOOL = "tool"
    QUERY_SOURCE = "query_source"
    RETRIEVAL = "retrieval"
    MEMORY = "memory"
    CACHE = "cache"
    STORAGE = "storage"
    TELEMETRY = "telemetry"
    SERVICE = "service"


class WorkflowResourceReference(BaseModel):
    """Portable, secret-free link from a workflow node to a configured Runtime resource."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: WorkflowResourceKind
    name: str = Field(min_length=1, max_length=128)
    access: Literal["read", "write", "read_write", "invoke", "observe"] = "read"
    description: str = ""


class WorkflowPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str
    operator: Literal[
        "equals", "not_equals", "contains", "not_contains", "truthy", "falsy", "in"
    ] = "truthy"
    value: Any = None


class WorkflowPauseRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    when: WorkflowPredicate
    question_from: str
    state_from: tuple[str, ...] = ()
    occurrences_from: str | None = None
    maximum_occurrences: int = Field(default=1, ge=0, le=20)


class WorkflowNode(BaseModel):
    """One Runtime-owned workflow node; domain behavior is named, never embedded code."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    kind: WorkflowNodeKind
    depends_on: tuple[str, ...] = ()
    output_key: str
    agent: str | None = None
    handler: str | None = None
    input_builder: str | None = None
    input_template: str | None = None
    metadata_builder: str | None = None
    validator: str | None = None
    output_schema: dict[str, Any] | None = None
    output_schema_hook: str | None = None
    max_repairs: int = Field(default=0, ge=0, le=10)
    map_from: str | None = None
    max_fan_out: int = Field(default=1, ge=1, le=128)
    run_if: WorkflowPredicate | None = None
    pause: WorkflowPauseRule | None = None
    condition: WorkflowPredicate | None = None
    join_strategy: Literal["concat", "json_array", "first"] = "concat"
    max_iterations: int = Field(default=1, ge=1, le=20)
    loop_until: WorkflowPredicate | None = None
    failure_policy: Literal["fail_workflow", "skip_dependents", "continue"] = "skip_dependents"
    resources: tuple[WorkflowResourceReference, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self) -> WorkflowNode:
        if self.kind in {WorkflowNodeKind.AGENT, WorkflowNodeKind.MAP_AGENT}:
            if not self.agent or not (self.input_builder or self.input_template):
                raise ValueError(
                    "agent workflow nodes require agent and input_builder or input_template"
                )
            if self.output_schema is not None and self.output_schema_hook is not None:
                raise ValueError("agent nodes accept output_schema or output_schema_hook, not both")
        elif self.kind == WorkflowNodeKind.HANDLER and not self.handler:
            raise ValueError("handler workflow nodes require handler")
        elif self.kind == WorkflowNodeKind.PREDICATE and self.condition is None:
            raise ValueError("predicate workflow nodes require condition")
        elif self.kind == WorkflowNodeKind.JOIN and not self.depends_on:
            raise ValueError("join workflow nodes require dependencies")
        if self.kind == WorkflowNodeKind.MAP_AGENT and not self.map_from:
            raise ValueError("map_agent workflow nodes require map_from")
        if self.kind != WorkflowNodeKind.MAP_AGENT and self.map_from is not None:
            raise ValueError("map_from is supported only by map_agent nodes")
        return self


class WorkflowManifest(BaseModel):
    """Portable, validated multi-agent workflow topology owned by Runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str
    description: str = ""
    hook_provider: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$",
        description=(
            "Optional application hook factory in module:function form. Hosts decide whether "
            "and how trusted providers are loaded."
        ),
    )
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1)
    maximum_concurrency: int = Field(default=8, ge=1, le=128)
    timeout_seconds: float = Field(default=300, gt=0, le=86_400)
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dag(self) -> WorkflowManifest:
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow node identifiers must be unique")
        known = set(ids)
        dependencies = {node.id: node.depends_on for node in self.nodes}
        for node in self.nodes:
            missing = set(node.depends_on) - known
            if missing:
                raise ValueError(f"node {node.id!r} has unknown dependencies {sorted(missing)}")
            if node.id in node.depends_on:
                raise ValueError("a workflow node cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("workflow contains a dependency cycle")
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


class WorkflowNodeExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str
    status: WorkflowNodeStatus = WorkflowNodeStatus.PENDING
    attempts: int = 0
    run_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: Any | None = None
    completed_at: Any | None = None


class WorkflowExecutionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    manifest_name: str
    manifest_version: str
    tenant_id: str
    user_id: str
    conversation_id: str | None = None
    turn_id: str | None = None
    status: WorkflowStatus = WorkflowStatus.PENDING
    values: dict[str, Any] = Field(default_factory=dict)
    nodes: dict[str, WorkflowNodeExecution] = Field(default_factory=dict)
    pause: dict[str, Any] | None = None
    error: str | None = None
    created_at: Any = Field(default_factory=utc_now)
    updated_at: Any = Field(default_factory=utc_now)


class WorkflowHookContext(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    state: WorkflowExecutionState
    node: WorkflowNode
    item: Any = None
    item_index: int | None = None
    invalid_output: Any = None
    validation_error: str | None = None
    request_payload: Any = None


PayloadBuilder = Callable[[WorkflowHookContext], Awaitable[Any] | Any]
OutputValidator = Callable[[Any, WorkflowHookContext], Awaitable[str | None] | str | None]
NodeHandler = Callable[[WorkflowHookContext], Awaitable[Any] | Any]
WorkflowEventSink = Callable[[str, dict[str, Any]], Awaitable[None] | None]
WorkflowAgentRunner = Callable[
    [Any, WorkflowHookContext],
    Awaitable[Any],
]
