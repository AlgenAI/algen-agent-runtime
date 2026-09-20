from __future__ import annotations

from algen_agent_runtime.exceptions.errors import ConfigurationError, NotFoundError
from algen_agent_runtime.semantics.contracts import (
    Aggregation,
    Certification,
    CompiledSemanticQuery,
    FilterOperator,
    MetricFilter,
    MetricQuery,
    Sensitivity,
)
from algen_agent_runtime.semantics.layer import SemanticLayer


def _identifier(value: str) -> str:
    return ".".join(f'"{part}"' for part in value.split("."))


class SemanticQueryCompiler:
    """Compiles one-model metric queries; values are always positional parameters."""

    def __init__(
        self,
        *,
        minimum_certification: Certification = "draft",
        maximum_sensitivity: Sensitivity = "restricted",
        authorization_tags: frozenset[str] = frozenset(),
    ) -> None:
        self._minimum_certification = minimum_certification
        self._maximum_sensitivity = maximum_sensitivity
        self._authorization_tags = authorization_tags

    def compile(self, layer: SemanticLayer, query: MetricQuery) -> CompiledSemanticQuery:
        layer.validate_query(
            query,
            minimum_certification=self._minimum_certification,
            maximum_sensitivity=self._maximum_sensitivity,
            authorization_tags=self._authorization_tags,
        )
        metrics = [layer.metric(name) for name in query.metrics]
        dimensions = [layer.dimension(name) for name in query.dimensions]
        model_names = {model for model, _ in (*metrics, *dimensions)}
        if len(model_names) != 1:
            raise ConfigurationError(
                "semantic compiler supports one model per query; use an explicit governed join plan"
            )
        if query.time_range:
            raise ConfigurationError(
                "normalize time_range into typed filters before semantic SQL compilation"
            )
        model_name = model_names.pop()
        model = next(item for item in layer.definition.models if item.name == model_name)
        select = [
            f"{dimension.expression} AS {_identifier(dimension.name)}"
            for _, dimension in dimensions
        ]
        select.extend(
            f"{self._aggregate(metric.aggregation, metric.expression)} AS {_identifier(metric.name)}"
            for _, metric in metrics
        )
        where: list[str] = []
        parameters: list[str | int | float | bool] = []
        filters = self._effective_filters(layer, model_name, query.filters)
        for metric_filter in filters:
            filter_model, dimension = layer.dimension(metric_filter.field)
            if filter_model != model_name:
                raise ConfigurationError(
                    f"filter {metric_filter.field!r} belongs to a different semantic model"
                )
            where.append(
                self._filter(
                    dimension.expression,
                    metric_filter.operator,
                    metric_filter.value,
                    parameters,
                )
            )
        if model.freshness:
            freshness_model, freshness_dimension = layer.dimension(model.freshness.field)
            if freshness_model != model_name:
                raise ConfigurationError(
                    f"freshness field {model.freshness.field!r} belongs to a different model"
                )
            parameters.append(model.freshness.max_age_seconds)
            where.append(
                f"{freshness_dimension.expression} >= CURRENT_TIMESTAMP - "
                f"(${len(parameters)} * INTERVAL '1 second')"
            )
        selected = {dimension.name for _, dimension in dimensions} | {
            metric.name for _, metric in metrics
        }
        order_parts = []
        for metric_sort in query.order_by:
            resolved = self._selected_name(layer, metric_sort.field)
            if resolved not in selected:
                raise ConfigurationError(f"order field {metric_sort.field!r} is not selected")
            order_parts.append(f"{_identifier(resolved)} {metric_sort.direction.value.upper()}")
        sql = "SELECT " + ", ".join(select) + f" FROM {_identifier(model.table)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if dimensions:
            sql += " GROUP BY " + ", ".join(item.expression for _, item in dimensions)
        if order_parts:
            sql += " ORDER BY " + ", ".join(order_parts)
        sql += f" LIMIT {query.limit};"
        return CompiledSemanticQuery(
            sql=sql,
            parameters=tuple(parameters),
            semantic_layer=layer.definition.name,
            semantic_layer_version=layer.definition.version,
            semantic_layer_digest=layer.digest,
            semantic_model=model_name,
        )

    @staticmethod
    def _effective_filters(
        layer: SemanticLayer,
        model_name: str,
        query_filters: tuple[MetricFilter, ...],
    ) -> tuple[MetricFilter, ...]:
        model = next(item for item in layer.definition.models if item.name == model_name)
        defaults = {item.field: item for item in model.default_filters}
        effective = list(model.default_filters)
        for item in query_filters:
            resolved_model, dimension = layer.dimension(item.field)
            if resolved_model != model_name:
                raise ConfigurationError(
                    f"filter {item.field!r} belongs to a different semantic model"
                )
            protected = defaults.get(dimension.name)
            if protected:
                if protected.operator != item.operator or protected.value != item.value:
                    raise ConfigurationError(
                        f"filter {dimension.name!r} cannot override its governed default"
                    )
                continue
            effective.append(item.model_copy(update={"field": dimension.name}))
        return tuple(effective)

    @staticmethod
    def _aggregate(aggregation: Aggregation, expression: str) -> str:
        if aggregation == Aggregation.EXPRESSION:
            return expression
        functions = {
            Aggregation.SUM: "SUM",
            Aggregation.AVG: "AVG",
            Aggregation.MIN: "MIN",
            Aggregation.MAX: "MAX",
            Aggregation.COUNT: "COUNT",
        }
        if aggregation == Aggregation.COUNT_DISTINCT:
            return f"COUNT(DISTINCT {expression})"
        return f"{functions[aggregation]}({expression})"

    @staticmethod
    def _filter(
        expression: str,
        operator: FilterOperator,
        value: str | int | float | bool | tuple[str | int | float | bool, ...] | None,
        parameters: list[str | int | float | bool],
    ) -> str:
        if operator == FilterOperator.IS_NULL:
            return f"{expression} IS NULL"
        if operator == FilterOperator.IS_NOT_NULL:
            return f"{expression} IS NOT NULL"
        values = value if isinstance(value, tuple) else (value,)
        placeholders = []
        for item in values:
            if item is None:
                raise ConfigurationError("null filter values require an is_null operator")
            parameters.append(item)
            placeholders.append(f"${len(parameters)}")
        if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
            keyword = "IN" if operator == FilterOperator.IN else "NOT IN"
            return f"{expression} {keyword} ({', '.join(placeholders)})"
        symbols = {
            FilterOperator.EQ: "=",
            FilterOperator.NE: "!=",
            FilterOperator.GT: ">",
            FilterOperator.GTE: ">=",
            FilterOperator.LT: "<",
            FilterOperator.LTE: "<=",
        }
        return f"{expression} {symbols[operator]} {placeholders[0]}"

    @staticmethod
    def _selected_name(layer: SemanticLayer, field: str) -> str:
        try:
            return layer.dimension(field)[1].name
        except NotFoundError:
            return layer.metric(field)[1].name
