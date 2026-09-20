import pytest

from algen_agent_runtime.analytics import AnalyticalNode, AnalyticalNodeKind, ResultReference
from algen_agent_runtime.exceptions import ConfigurationError
from algen_agent_runtime.semantics import (
    Aggregation,
    DimensionDefinition,
    JoinDefinition,
    MetricBehavior,
    MetricDefinition,
    MetricQuery,
    PeriodComparison,
    ProductionSemanticQueryCompiler,
    RelativePeriod,
    SemanticAnalysisPlan,
    SemanticLayer,
    SemanticLayerDefinition,
    SemanticModelDefinition,
    SemanticQueryGraphPlanner,
    SemanticQueryTask,
    SemanticType,
    TimeGrain,
)


def _commercial_layer() -> SemanticLayer:
    return SemanticLayer(
        SemanticLayerDefinition(
            name="commercial",
            version="2",
            description="test",
            models=(
                SemanticModelDefinition(
                    name="sales",
                    description="sales",
                    table="analytics.sales",
                    primary_key=("id",),
                    dimensions=(
                        DimensionDefinition(
                            name="flight_date",
                            label="Date",
                            description="date",
                            expression="flight_date",
                            type=SemanticType.DATE,
                            is_time=True,
                        ),
                    ),
                    metrics=(
                        MetricDefinition(
                            name="average_fare",
                            label="Fare",
                            description="ratio",
                            expression="revenue",
                            aggregation=Aggregation.EXPRESSION,
                            behavior=MetricBehavior.RATIO,
                            numerator="revenue",
                            denominator="bookings",
                            allowed_dimensions=("flight_date", "region"),
                            certification="verified",
                        ),
                    ),
                    certification="verified",
                ),
                SemanticModelDefinition(
                    name="region",
                    description="region",
                    table="analytics.regions",
                    primary_key=("id",),
                    dimensions=(
                        DimensionDefinition(
                            name="region",
                            label="Region",
                            description="region",
                            expression="name",
                            type=SemanticType.STRING,
                        ),
                    ),
                    metrics=(),
                    certification="verified",
                ),
            ),
            joins=(
                JoinDefinition(
                    name="sales_region",
                    left_model="sales",
                    right_model="region",
                    relationship="many_to_one",
                    sql_on="m0.region_id = m1.id",
                    description="governed region lookup",
                ),
            ),
        )
    )


def test_production_compiler_handles_ratio_time_and_governed_many_to_one_join() -> None:
    layer = _commercial_layer()
    compiled = ProductionSemanticQueryCompiler().compile(
        layer,
        MetricQuery(
            metrics=("average_fare",),
            dimensions=("flight_date", "region"),
            time_grain=TimeGrain.WEEK,
            relative_period=RelativePeriod(field="flight_date", unit=TimeGrain.DAY, count=30),
        ),
    )
    assert "DATE_TRUNC('week', m0.flight_date)" in compiled.sql
    assert "SUM(m0.revenue) / NULLIF(SUM(m0.bookings), 0)" in compiled.sql
    assert 'LEFT JOIN "analytics"."regions" AS m1' in compiled.sql
    assert compiled.joins == ("sales_region",)


def test_advanced_semantics_must_be_lowered_instead_of_silently_ignored() -> None:
    with pytest.raises(ConfigurationError, match="compare_periods graph node"):
        ProductionSemanticQueryCompiler().compile(
            _commercial_layer(),
            MetricQuery(
                metrics=("average_fare",),
                dimensions=("flight_date",),
                comparison=PeriodComparison(mode="previous_year"),
            ),
        )


def test_simple_ratio_uses_production_aggregation_path() -> None:
    compiled = ProductionSemanticQueryCompiler().compile(
        _commercial_layer(), MetricQuery(metrics=("average_fare",))
    )
    assert "SUM(m0.revenue) / NULLIF(SUM(m0.bookings), 0)" in compiled.sql


def test_semantic_graph_planner_compiles_separate_results_and_transformations() -> None:
    comparison = AnalyticalNode(
        id="compare",
        kind=AnalyticalNodeKind.COMPARE_PERIODS,
        dependencies=("current", "baseline"),
        inputs={
            "current": ResultReference(node_id="current"),
            "baseline": ResultReference(node_id="baseline"),
        },
        configuration={"metric": "average_fare", "keys": ["flight_date"]},
    )
    graph = SemanticQueryGraphPlanner(ProductionSemanticQueryCompiler()).plan(
        _commercial_layer(),
        SemanticAnalysisPlan(
            name="fare-comparison",
            version="1",
            queries=(
                SemanticQueryTask(
                    id="current",
                    query=MetricQuery(metrics=("average_fare",), dimensions=("flight_date",)),
                ),
                SemanticQueryTask(
                    id="baseline",
                    query=MetricQuery(metrics=("average_fare",), dimensions=("flight_date",)),
                ),
            ),
            transformations=(comparison,),
        ),
    )
    assert tuple(node.id for node in graph.nodes) == ("current", "baseline", "compare")
    assert graph.nodes[0].configuration["compiled_query"]["semantic_layer_digest"]
