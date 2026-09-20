from __future__ import annotations

import asyncio

import pytest

from algen_agent_runtime.analytics import (
    AnalyticalGraph,
    AnalyticalGraphEngine,
    AnalyticalNode,
    AnalyticalNodeKind,
    AnalyticalNodeRegistry,
    AnalyticalResult,
    GraphExecutionContext,
    GraphStatus,
    InMemoryAnalyticalGraphStore,
    NodeBudget,
    ResultReference,
    builtin_handlers,
)
from algen_agent_runtime.exceptions.errors import BudgetExceededError


def _engine() -> AnalyticalGraphEngine:
    registry = AnalyticalNodeRegistry()
    for kind, handler in builtin_handlers().items():
        registry.register(AnalyticalNodeKind(kind), handler)

    async def query(node, inputs, context):
        del inputs, context
        await asyncio.sleep(float(node.configuration.get("delay", 0)))
        rows = node.configuration["rows"]
        return AnalyticalResult(node_id=node.id, value=rows, row_count=len(rows))

    registry.register(AnalyticalNodeKind.SEMANTIC_QUERY, query)
    return AnalyticalGraphEngine(registry, InMemoryAnalyticalGraphStore())


def test_engine_exposes_application_handler_registration() -> None:
    registry = AnalyticalNodeRegistry()
    engine = AnalyticalGraphEngine(registry, InMemoryAnalyticalGraphStore())

    async def semantic_query(node, inputs, context):  # type: ignore[no-untyped-def]
        del inputs, context
        return AnalyticalResult(node_id=node.id, value=[])

    engine.register_handler(AnalyticalNodeKind.SEMANTIC_QUERY, semantic_query)

    assert AnalyticalNodeKind.SEMANTIC_QUERY in engine.handler_kinds()


async def test_graph_keeps_query_results_separate_and_joins_by_reference() -> None:
    graph = AnalyticalGraph(
        name="comparison",
        version="1",
        nodes=(
            AnalyticalNode(
                id="sales",
                kind="semantic_query",
                configuration={"rows": [{"route": "A-B", "revenue": 10}]},
            ),
            AnalyticalNode(
                id="capacity",
                kind="semantic_query",
                configuration={"rows": [{"route": "A-B", "seats": 2}]},
            ),
            AnalyticalNode(
                id="joined",
                kind="join_results",
                dependencies=("sales", "capacity"),
                inputs={
                    "left": ResultReference(node_id="sales"),
                    "right": ResultReference(node_id="capacity"),
                },
                configuration={"keys": ["route"], "relationship": "one_to_one"},
            ),
        ),
    )
    state = await _engine().run(graph, GraphExecutionContext(tenant_id="t1", user_id="u1"))

    assert state.status == GraphStatus.COMPLETED
    assert set(state.results) == {"sales", "capacity", "joined"}
    assert state.results["joined"].value == [{"route": "A-B", "revenue": 10, "seats": 2}]


def test_graph_rejects_cycles_and_undeclared_result_references() -> None:
    with pytest.raises(ValueError, match="declared as a dependency"):
        AnalyticalNode(
            id="b",
            kind="calculate",
            inputs={"rows": ResultReference(node_id="a")},
        )
    with pytest.raises(ValueError, match="cycle"):
        AnalyticalGraph(
            name="cycle",
            version="1",
            nodes=(
                AnalyticalNode(id="a", kind="compose", dependencies=("b",)),
                AnalyticalNode(id="b", kind="compose", dependencies=("a",)),
            ),
        )


async def test_deployment_timeout_caps_application_graph_timeout() -> None:
    registry = AnalyticalNodeRegistry()

    async def slow_query(node, inputs, context):
        del node, inputs, context
        await asyncio.sleep(0.1)
        return AnalyticalResult(node_id="query", value=[])

    registry.register(AnalyticalNodeKind.SEMANTIC_QUERY, slow_query)
    engine = AnalyticalGraphEngine(
        registry,
        InMemoryAnalyticalGraphStore(),
        maximum_graph_timeout_seconds=0.01,
    )
    graph = AnalyticalGraph(
        name="bounded",
        version="1",
        timeout_seconds=30,
        nodes=(
            AnalyticalNode(
                id="query",
                kind="semantic_query",
                budget=NodeBudget(timeout_seconds=10),
            ),
        ),
    )
    with pytest.raises(BudgetExceededError, match="graph timeout"):
        await engine.run(graph, GraphExecutionContext(tenant_id="t1", user_id="u1"))
