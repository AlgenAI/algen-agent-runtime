from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Mapping
from time import monotonic
from typing import Any

import jsonschema
from opentelemetry import metrics, trace

from algen_agent_runtime.analytics.contracts import (
    AnalyticalGraph,
    AnalyticalGraphState,
    AnalyticalGraphStore,
    AnalyticalNode,
    AnalyticalNodeHandler,
    AnalyticalNodeKind,
    AnalyticalResult,
    GraphExecutionContext,
    GraphStatus,
    NodeExecution,
    NodeStatus,
    ResultProvenance,
)
from algen_agent_runtime.cache import CacheContext, CacheService
from algen_agent_runtime.exceptions.errors import BudgetExceededError, ConfigurationError
from algen_agent_runtime.types.contracts import utc_now


class AnalyticalNodeRegistry:
    def __init__(self) -> None:
        self._handlers: dict[AnalyticalNodeKind, AnalyticalNodeHandler] = {}

    def register(self, kind: AnalyticalNodeKind, handler: AnalyticalNodeHandler) -> None:
        if kind in self._handlers:
            raise ConfigurationError(f"handler for analytical node {kind.value!r} is registered")
        self._handlers[kind] = handler

    def get(self, kind: AnalyticalNodeKind) -> AnalyticalNodeHandler:
        try:
            return self._handlers[kind]
        except KeyError as exc:
            raise ConfigurationError(f"no handler for analytical node {kind.value!r}") from exc

    def kinds(self) -> tuple[AnalyticalNodeKind, ...]:
        return tuple(sorted(self._handlers, key=str))


class AnalyticalGraphEngine:
    """Checkpointed DAG executor that keeps each node result independently addressable."""

    def __init__(
        self,
        registry: AnalyticalNodeRegistry,
        store: AnalyticalGraphStore,
        *,
        cache: CacheService | None = None,
        maximum_concurrency: int = 8,
        maximum_node_timeout_seconds: float = 30,
        maximum_graph_timeout_seconds: float = 300,
    ) -> None:
        self._registry = registry
        self._store = store
        self._cache = cache
        self._maximum_concurrency = maximum_concurrency
        self._maximum_node_timeout_seconds = maximum_node_timeout_seconds
        self._maximum_graph_timeout_seconds = maximum_graph_timeout_seconds
        self._tracer = trace.get_tracer("algen_agent_runtime.analytics")
        meter = metrics.get_meter("algen_agent_runtime.analytics")
        self._node_duration = meter.create_histogram(
            "algen_agent_runtime.analytics.node.duration", unit="ms"
        )
        self._node_runs = meter.create_counter("algen_agent_runtime.analytics.node.executions")
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._checkpoint_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._concurrency_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def register_handler(
        self,
        kind: AnalyticalNodeKind,
        handler: AnalyticalNodeHandler,
    ) -> None:
        """Register an application-owned node handler on the composed graph engine."""
        self._registry.register(kind, handler)

    def handler_kinds(self) -> tuple[AnalyticalNodeKind, ...]:
        """Return node kinds available in this composed engine."""
        return self._registry.kinds()

    async def run(
        self, graph: AnalyticalGraph, context: GraphExecutionContext
    ) -> AnalyticalGraphState:
        state = AnalyticalGraphState(
            id=graph.id,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            graph=graph,
            nodes={node.id: NodeExecution(node_id=node.id) for node in graph.nodes},
        )
        await self._store.create(state)
        return await self._drive(state, context)

    async def resume(self, graph_id: str, context: GraphExecutionContext) -> AnalyticalGraphState:
        state = await self._store.get(graph_id, context.tenant_id)
        if state is None:
            raise ConfigurationError(f"analytical graph {graph_id!r} was not found")
        if state.user_id != context.user_id:
            raise ConfigurationError("analytical graph belongs to another user")
        for execution in state.nodes.values():
            if execution.status == NodeStatus.RUNNING:
                execution.status = NodeStatus.PENDING
                execution.error = "recovered after an interrupted worker lease"
        if state.status == GraphStatus.RUNNING:
            state.status = GraphStatus.PENDING
        return await self._drive(state, context)

    async def _drive(
        self, state: AnalyticalGraphState, context: GraphExecutionContext
    ) -> AnalyticalGraphState:
        async with self._locks[state.id]:
            with self._tracer.start_as_current_span(
                "analytical_graph.run",
                attributes={
                    "analytics.graph.id": state.id,
                    "analytics.graph.name": state.graph.name,
                    "analytics.graph.version": state.graph.version,
                    "tenant.id": context.tenant_id,
                },
            ):
                state.status = GraphStatus.RUNNING
                await self._checkpoint(state)
                try:
                    async with asyncio.timeout(
                        min(state.graph.timeout_seconds, self._maximum_graph_timeout_seconds)
                    ):
                        await self._schedule(state, context)
                except TimeoutError:
                    state.status = GraphStatus.FAILED
                    for execution in state.nodes.values():
                        if execution.status in {NodeStatus.PENDING, NodeStatus.RUNNING}:
                            execution.status = NodeStatus.CANCELLED
                            execution.error = "graph timeout exhausted"
                    await self._checkpoint(state)
                    raise BudgetExceededError("analytical graph timeout exhausted") from None
                except asyncio.CancelledError:
                    state.status = GraphStatus.CANCELLED
                    await self._checkpoint(state)
                    raise
                except Exception:
                    state.status = GraphStatus.FAILED
                    await self._checkpoint(state)
                    raise
                state.status = GraphStatus.COMPLETED
                await self._checkpoint(state)
                return state

    async def _schedule(self, state: AnalyticalGraphState, context: GraphExecutionContext) -> None:
        nodes = {node.id: node for node in state.graph.nodes}
        semaphore = asyncio.Semaphore(
            min(state.graph.maximum_concurrency, self._maximum_concurrency)
        )
        while True:
            incomplete = {
                node_id
                for node_id, execution in state.nodes.items()
                if execution.status != NodeStatus.COMPLETED
            }
            if not incomplete:
                return
            failed = [
                node_id
                for node_id in incomplete
                if state.nodes[node_id].status in {NodeStatus.FAILED, NodeStatus.CANCELLED}
            ]
            if failed:
                raise RuntimeError(f"analytical graph failed at nodes {sorted(failed)}")
            ready = [
                nodes[node_id]
                for node_id in incomplete
                if state.nodes[node_id].status == NodeStatus.PENDING
                and all(
                    state.nodes[item].status == NodeStatus.COMPLETED
                    for item in nodes[node_id].dependencies
                )
            ]
            if not ready:
                raise RuntimeError("analytical graph has no runnable nodes")

            async def execute(node: AnalyticalNode) -> None:
                async with semaphore:
                    if node.concurrency_key:
                        async with self._concurrency_locks[node.concurrency_key]:
                            await self._execute_node(state, node, context)
                    else:
                        await self._execute_node(state, node, context)

            outcomes = await asyncio.gather(
                *(execute(node) for node in ready), return_exceptions=True
            )
            errors = [item for item in outcomes if isinstance(item, BaseException)]
            if errors:
                raise errors[0]

    async def _execute_node(
        self,
        state: AnalyticalGraphState,
        node: AnalyticalNode,
        context: GraphExecutionContext,
    ) -> None:
        execution = state.nodes[node.id]
        execution.status = NodeStatus.RUNNING
        execution.started_at = utc_now()
        await self._checkpoint(state)
        inputs = self._resolve_inputs(state, node)
        jsonschema.validate(inputs, node.input_schema)
        handler = self._registry.get(node.kind)
        started = monotonic()
        attributes = {
            "analytics.graph.id": state.id,
            "analytics.node.id": node.id,
            "analytics.node.kind": node.kind.value,
        }
        with self._tracer.start_as_current_span(
            f"analytical_node.{node.kind.value}", attributes=attributes
        ) as span:
            try:
                cached = await self._cached_result(state, node, context, inputs)
                if cached is not None:
                    result = cached
                    span.set_attribute("cache.hit", True)
                else:
                    result = await self._invoke(handler, node, inputs, context, execution)
                    await self._validate_result(node, result)
                    await self._store_result_cache(state, node, context, inputs, result)
                    span.set_attribute("cache.hit", False)
                if result.node_id != node.id:
                    result = result.model_copy(update={"node_id": node.id})
                self._enforce_result_budget(node, result)
                state.results[node.id] = result
                execution.result_id = result.id
                execution.status = NodeStatus.COMPLETED
                execution.completed_at = utc_now()
                self._node_runs.add(1, {**attributes, "outcome": "completed"})
            except BaseException as exc:
                execution.status = NodeStatus.FAILED
                execution.error = f"{type(exc).__name__}: {exc}"
                execution.completed_at = utc_now()
                span.record_exception(exc)
                self._node_runs.add(1, {**attributes, "outcome": "failed"})
                raise
            finally:
                self._node_duration.record((monotonic() - started) * 1000, attributes)
                await self._checkpoint(state)

    async def _invoke(
        self,
        handler: AnalyticalNodeHandler,
        node: AnalyticalNode,
        inputs: dict[str, Any],
        context: GraphExecutionContext,
        execution: NodeExecution,
    ) -> AnalyticalResult:
        last_error: BaseException | None = None
        for attempt in range(1, node.retry.maximum_attempts + 1):
            execution.attempts = attempt
            try:
                async with asyncio.timeout(
                    min(node.budget.timeout_seconds, self._maximum_node_timeout_seconds)
                ):
                    return await handler(node, inputs, context)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                last_error = exc
                if attempt < node.retry.maximum_attempts:
                    delay = min(
                        node.retry.maximum_backoff_seconds,
                        node.retry.initial_backoff_seconds * (2 ** (attempt - 1)),
                    )
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    def _resolve_inputs(state: AnalyticalGraphState, node: AnalyticalNode) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for name, reference in node.inputs.items():
            result = state.results[reference.node_id]
            value = result.value
            if reference.path:
                for part in reference.path.split("."):
                    if isinstance(value, Mapping):
                        value = value[part]
                    elif isinstance(value, list):
                        value = value[int(part)]
                    else:
                        raise ConfigurationError(
                            f"result path {reference.path!r} cannot traverse {type(value).__name__}"
                        )
            resolved[name] = value
        return resolved

    @staticmethod
    async def _validate_result(node: AnalyticalNode, result: AnalyticalResult) -> None:
        if node.output_schema:
            jsonschema.validate(result.value, node.output_schema)

    @staticmethod
    def _enforce_result_budget(node: AnalyticalNode, result: AnalyticalResult) -> None:
        encoded = json.dumps(result.value, default=str).encode()
        if len(encoded) > node.budget.maximum_bytes:
            raise BudgetExceededError(f"node {node.id!r} exceeded result byte budget")
        if result.row_count is not None and result.row_count > node.budget.maximum_rows:
            raise BudgetExceededError(f"node {node.id!r} exceeded result row budget")
        if (
            node.budget.maximum_cost_usd is not None
            and result.cost_usd > node.budget.maximum_cost_usd
        ):
            raise BudgetExceededError(f"node {node.id!r} exceeded result cost budget")

    async def _cached_result(
        self,
        state: AnalyticalGraphState,
        node: AnalyticalNode,
        context: GraphExecutionContext,
        inputs: dict[str, Any],
    ) -> AnalyticalResult | None:
        if self._cache is None or not node.cache.enabled:
            return None
        value = await self._cache.get_json(
            node.cache.policy_name,
            f"analytical-node.{state.graph.name}.{node.kind.value}",
            self._cache_material(state, node, inputs),
            self._cache_context(context),
        )
        return AnalyticalResult.model_validate(value) if value is not None else None

    async def _store_result_cache(
        self,
        state: AnalyticalGraphState,
        node: AnalyticalNode,
        context: GraphExecutionContext,
        inputs: dict[str, Any],
        result: AnalyticalResult,
    ) -> None:
        if self._cache is None or not node.cache.enabled:
            return
        await self._cache.set_json(
            node.cache.policy_name,
            f"analytical-node.{state.graph.name}.{node.kind.value}",
            self._cache_material(state, node, inputs),
            result.model_dump(mode="json", by_alias=True),
            self._cache_context(context),
            tags=(f"graph:{state.graph.name}",),
        )

    @staticmethod
    def _cache_material(
        state: AnalyticalGraphState, node: AnalyticalNode, inputs: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "graph_version": state.graph.version,
            "node": node.model_dump(mode="json"),
            "inputs": inputs,
        }

    @staticmethod
    def _cache_context(context: GraphExecutionContext) -> CacheContext:
        return CacheContext(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            run_id=context.run_id,
            authorization_fingerprint=context.authorization_fingerprint,
        )

    async def _checkpoint(self, state: AnalyticalGraphState) -> None:
        async with self._checkpoint_locks[state.id]:
            state.updated_at = utc_now()
            await self._store.save(state, state.version)


async def passthrough_handler(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    value = inputs if inputs else node.configuration.get("value")
    return AnalyticalResult(
        node_id=node.id,
        value=value,
        row_count=len(value) if isinstance(value, list) else None,
        provenance=ResultProvenance(
            upstream_result_ids=tuple(reference.node_id for reference in node.inputs.values())
        ),
    )
