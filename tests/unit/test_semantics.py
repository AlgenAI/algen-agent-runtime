from __future__ import annotations

import pytest

from algen_agent_runtime.exceptions.errors import ConfigurationError, NotFoundError
from algen_agent_runtime.semantics import (
    Aggregation,
    DimensionDefinition,
    FilterOperator,
    FreshnessPolicy,
    MetricDefinition,
    MetricFilter,
    MetricQuery,
    MetricSort,
    SemanticLayer,
    SemanticLayerDefinition,
    SemanticModelDefinition,
    SemanticQueryCompiler,
    SemanticType,
    SortDirection,
)


def _layer(*, allowed_dimensions: tuple[str, ...] = ("route",)) -> SemanticLayer:
    return SemanticLayer(
        SemanticLayerDefinition(
            name="sales",
            version="1.0.0",
            description="Governed sales metrics",
            models=(
                SemanticModelDefinition(
                    name="orders",
                    description="Orders",
                    table="analytics.orders",
                    primary_key=("id",),
                    default_time_dimension=None,
                    dimensions=(
                        DimensionDefinition(
                            name="route",
                            label="Route",
                            description="Origin and destination",
                            expression="route",
                            type=SemanticType.STRING,
                            synonyms=("sector",),
                        ),
                        DimensionDefinition(
                            name="region",
                            label="Region",
                            description="Commercial region",
                            expression="region",
                            type=SemanticType.STRING,
                        ),
                    ),
                    metrics=(
                        MetricDefinition(
                            name="revenue",
                            label="Revenue",
                            description="Recognized revenue",
                            expression="amount",
                            aggregation=Aggregation.SUM,
                            synonyms=("sales",),
                            allowed_dimensions=allowed_dimensions,
                        ),
                    ),
                ),
            ),
        )
    )


def test_semantic_layer_resolves_aliases_and_has_stable_digest() -> None:
    first = _layer()
    second = _layer()

    assert first.metric("Sales")[1].name == "revenue"
    assert first.dimension("sector")[1].name == "route"
    assert first.metric_names() == ("revenue",)
    assert first.dimension_names() == ("route", "region")
    assert first.digest == second.digest
    assert first.definition.name in first.prompt_context()


def test_semantic_layer_accepts_unambiguous_model_qualified_references() -> None:
    layer = _layer()

    assert layer.metric("orders.revenue")[1].name == "revenue"
    assert layer.dimension("orders.route")[1].name == "route"
    with pytest.raises(NotFoundError, match="not defined"):
        layer.metric("another_model.revenue")


def test_semantic_query_rejects_unknown_or_disallowed_dimensions() -> None:
    layer = _layer(allowed_dimensions=())
    with pytest.raises(NotFoundError, match="not defined"):
        layer.validate_query(MetricQuery(metrics=("missing",)))

    restricted = _layer()
    with pytest.raises(ConfigurationError, match="cannot be grouped"):
        restricted.validate_query(MetricQuery(metrics=("revenue",), dimensions=("region",)))


def test_semantic_layer_rejects_ambiguous_aliases() -> None:
    definition = _layer().definition
    duplicate = definition.models[0].model_copy(
        update={
            "metrics": (
                definition.models[0].metrics[0],
                definition.models[0]
                .metrics[0]
                .model_copy(update={"name": "net_revenue", "synonyms": ("sales",)}),
            )
        }
    )
    with pytest.raises(ConfigurationError, match="ambiguous semantic alias"):
        SemanticLayer(definition.model_copy(update={"models": (duplicate,)}))


def test_semantic_compiler_builds_parameterized_governed_sql() -> None:
    compiled = SemanticQueryCompiler().compile(
        _layer(),
        MetricQuery(
            metrics=("sales",),
            dimensions=("sector",),
            filters=(
                MetricFilter(
                    field="sector",
                    operator=FilterOperator.IN,
                    value=("DEL-BOM", "DEL-BLR"),
                ),
            ),
            order_by=(MetricSort(field="sales", direction=SortDirection.DESC),),
            limit=25,
        ),
    )

    assert compiled.sql == (
        'SELECT route AS "route", SUM(amount) AS "revenue" FROM "analytics"."orders" '
        'WHERE route IN ($1, $2) GROUP BY route ORDER BY "revenue" DESC LIMIT 25;'
    )
    assert compiled.parameters == ("DEL-BOM", "DEL-BLR")
    assert compiled.semantic_model == "orders"


def test_semantic_compiler_enforces_default_and_required_filters() -> None:
    definition = _layer().definition
    base = definition.models[0]
    governed = base.model_copy(
        update={
            "dimensions": (
                *base.dimensions,
                DimensionDefinition(
                    name="snapshot_date",
                    label="Snapshot date",
                    description="Required point-in-time selection",
                    expression="snapshot_date",
                    type=SemanticType.DATE,
                    is_time=True,
                ),
                DimensionDefinition(
                    name="is_current",
                    label="Current row",
                    description="Governed current-version flag",
                    expression="is_current",
                    type=SemanticType.BOOLEAN,
                ),
            ),
            "default_filters": (MetricFilter(field="is_current", value=True),),
            "required_filter_dimensions": ("snapshot_date",),
        }
    )
    layer = SemanticLayer(definition.model_copy(update={"models": (governed,)}))

    with pytest.raises(ConfigurationError, match="requires filters"):
        SemanticQueryCompiler().compile(layer, MetricQuery(metrics=("revenue",)))

    compiled = SemanticQueryCompiler().compile(
        layer,
        MetricQuery(
            metrics=("revenue",),
            filters=(
                MetricFilter(
                    field="snapshot_date",
                    operator=FilterOperator.GTE,
                    value="2026-09-01",
                ),
            ),
        ),
    )
    assert "is_current = $1" in compiled.sql
    assert "snapshot_date >= $2" in compiled.sql
    assert compiled.parameters == (True, "2026-09-01")

    with pytest.raises(ConfigurationError, match="cannot override"):
        SemanticQueryCompiler().compile(
            layer,
            MetricQuery(
                metrics=("revenue",),
                filters=(
                    MetricFilter(field="snapshot_date", value="2026-09-01"),
                    MetricFilter(field="is_current", value=False),
                ),
            ),
        )


def test_semantic_compiler_enforces_governance_policy() -> None:
    definition = _layer().definition
    base = definition.models[0]
    governed_metric = base.metrics[0].model_copy(
        update={
            "certification": "verified",
            "sensitivity": "confidential",
            "authorization_tags": ("finance.read",),
        }
    )
    governed = base.model_copy(
        update={
            "certification": "verified",
            "sensitivity": "confidential",
            "metrics": (governed_metric,),
        }
    )
    layer = SemanticLayer(definition.model_copy(update={"models": (governed,)}))

    with pytest.raises(ConfigurationError, match="sensitivity"):
        SemanticQueryCompiler(
            minimum_certification="verified",
            maximum_sensitivity="internal",
            authorization_tags=frozenset({"finance.read"}),
        ).compile(layer, MetricQuery(metrics=("revenue",)))

    with pytest.raises(ConfigurationError, match="authorization tags"):
        SemanticQueryCompiler(
            minimum_certification="verified",
            maximum_sensitivity="confidential",
        ).compile(layer, MetricQuery(metrics=("revenue",)))

    compiled = SemanticQueryCompiler(
        minimum_certification="verified",
        maximum_sensitivity="confidential",
        authorization_tags=frozenset({"finance.read"}),
    ).compile(layer, MetricQuery(metrics=("revenue",)))
    assert 'SUM(amount) AS "revenue"' in compiled.sql


def test_semantic_compiler_applies_freshness_policy() -> None:
    definition = _layer().definition
    base = definition.models[0]
    governed = base.model_copy(
        update={
            "dimensions": (
                *base.dimensions,
                DimensionDefinition(
                    name="synced_at",
                    label="Synchronized at",
                    description="Source refresh timestamp",
                    expression="synced_at",
                    type=SemanticType.DATETIME,
                    is_time=True,
                ),
            ),
            "freshness": FreshnessPolicy(field="synced_at", max_age_seconds=3600),
        }
    )
    layer = SemanticLayer(definition.model_copy(update={"models": (governed,)}))

    compiled = SemanticQueryCompiler().compile(layer, MetricQuery(metrics=("revenue",)))
    assert "synced_at >= CURRENT_TIMESTAMP - ($1 * INTERVAL '1 second')" in compiled.sql
    assert compiled.parameters == (3600,)
