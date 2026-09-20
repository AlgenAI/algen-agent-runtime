from __future__ import annotations

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.semantics.compiler import SemanticQueryCompiler, _identifier
from algen_agent_runtime.semantics.contracts import (
    AnalyticalOperation,
    Certification,
    CompiledSemanticQuery,
    JoinDefinition,
    MetricBehavior,
    MetricDefinition,
    MetricFilter,
    MetricQuery,
    NullBehavior,
    SemanticModelDefinition,
    Sensitivity,
    TimeGrain,
)
from algen_agent_runtime.semantics.layer import SemanticLayer


class ProductionSemanticQueryCompiler(SemanticQueryCompiler):
    """PostgreSQL compiler for governed joins, time, snapshots, ratios and windows."""

    def __init__(
        self,
        *,
        minimum_certification: Certification = "verified",
        maximum_sensitivity: Sensitivity = "restricted",
        authorization_tags: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(
            minimum_certification=minimum_certification,
            maximum_sensitivity=maximum_sensitivity,
            authorization_tags=authorization_tags,
        )
        self._minimum_certification = minimum_certification
        self._maximum_sensitivity = maximum_sensitivity
        self._authorization_tags = authorization_tags

    def compile(self, layer: SemanticLayer, query: MetricQuery) -> CompiledSemanticQuery:
        self._validate_compilation_shape(query)
        layer.validate_query(
            query,
            minimum_certification=self._minimum_certification,
            maximum_sensitivity=self._maximum_sensitivity,
            authorization_tags=self._authorization_tags,
        )
        metrics = [layer.metric(name) for name in query.metrics]
        dimensions = [layer.dimension(name) for name in query.dimensions]
        selected_dimension_names = {dimension.name for _, dimension in dimensions}
        for _, metric in metrics:
            if (
                metric.behavior == MetricBehavior.SEMI_ADDITIVE
                and not set(metric.semi_additive_dimensions).issubset(selected_dimension_names)
                and query.snapshot is None
            ):
                raise ConfigurationError(
                    f"semi-additive metric {metric.name!r} requires its dimensions "
                    f"{sorted(metric.semi_additive_dimensions)} or an explicit snapshot"
                )
        model_names = tuple(dict.fromkeys(model for model, _ in (*metrics, *dimensions)))
        selected_models = tuple(self._model(layer, name) for name in model_names)
        needs_production_path = any(
            metric.behavior != MetricBehavior.ADDITIVE
            or metric.null_behavior != NullBehavior.PRESERVE
            for _, metric in metrics
        ) or any(
            model.valid_from_dimension or model.valid_to_dimension or model.late_arrival_seconds
            for model in selected_models
        )
        if len(model_names) == 1 and not self._advanced(query) and not needs_production_path:
            return super().compile(layer, query)
        metric_models = {model for model, _ in metrics}
        if len(metric_models) != 1:
            raise ConfigurationError(
                "multi-fact metric queries must be split into separate graph result sets and joined explicitly"
            )
        base_model = next(iter(metric_models))
        aliases = {name: f"m{index}" for index, name in enumerate(model_names)}
        joins = self._join_path(layer, base_model, set(model_names) - {base_model})
        select: list[str] = []
        group_by: list[str] = []
        explanation = [f"base fact model: {base_model}"]
        for model_name, dimension in dimensions:
            expression = self._expression(dimension.expression, aliases[model_name])
            if dimension.is_time and query.time_grain:
                expression = self._time_bucket(expression, query.time_grain)
            select.append(f"{expression} AS {_identifier(dimension.name)}")
            group_by.append(expression)
        for model_name, metric in metrics:
            expression = self._metric_expression(metric, aliases[model_name])
            if query.operation == AnalyticalOperation.CONTRIBUTION:
                expression = f"({expression}) / NULLIF(SUM({expression}) OVER (), 0)"
            select.append(f"{expression} AS {_identifier(metric.name)}")
        parameters: list[str | int | float | bool] = []
        where: list[str] = []
        effective_filters: list[MetricFilter] = []
        for model_name in model_names:
            model = self._model(layer, model_name)
            effective_filters.extend(model.default_filters)
        effective_filters.extend(query.filters)
        seen_filters: set[tuple[str, str]] = set()
        for item in effective_filters:
            model_name, dimension = layer.dimension(item.field)
            key = (model_name, dimension.name)
            if key in seen_filters:
                continue
            seen_filters.add(key)
            where.append(
                self._filter(
                    self._expression(dimension.expression, aliases[model_name]),
                    item.operator,
                    item.value,
                    parameters,
                )
            )
        if query.relative_period:
            model_name, dimension = layer.dimension(query.relative_period.field)
            parameters.append(query.relative_period.count)
            interval = query.relative_period.unit.value
            comparator = "<=" if query.relative_period.include_current else "<"
            expression = self._expression(dimension.expression, aliases[model_name])
            where.extend(
                (
                    f"{expression} >= CURRENT_DATE - (${len(parameters)} * INTERVAL '1 {interval}')",
                    f"{expression} {comparator} CURRENT_DATE",
                )
            )
            explanation.append(f"relative period: last {query.relative_period.count} {interval}(s)")
        for model_name in model_names:
            model = self._model(layer, model_name)
            if model.freshness:
                _, dimension = layer.dimension(model.freshness.field)
                expression = self._expression(dimension.expression, aliases[model_name])
                parameters.append(model.freshness.max_age_seconds + model.late_arrival_seconds)
                where.append(
                    f"{expression} >= CURRENT_TIMESTAMP - "
                    f"(${len(parameters)} * INTERVAL '1 second')"
                )
                explanation.append(
                    f"freshness enforced for {model_name}: "
                    f"{model.freshness.max_age_seconds}s maximum age"
                )
        if query.snapshot:
            model_name, dimension = layer.dimension(query.snapshot.field)
            expression = self._expression(dimension.expression, aliases[model_name])
            if query.snapshot.mode == "latest":
                where.append(
                    f"{expression} = (SELECT MAX(s.{dimension.expression}) FROM "
                    f"{_identifier(self._model(layer, model_name).table)} AS s)"
                )
            elif query.snapshot.mode == "as_of":
                parameters.append(query.snapshot.as_of or "")
                where.append(f"{expression} <= ${len(parameters)}")
            else:
                raise ConfigurationError(
                    "same-DTD snapshots require an explicit compare_periods graph node"
                )
        for model_name in model_names:
            model = self._model(layer, model_name)
            if not (model.valid_from_dimension and model.valid_to_dimension):
                continue
            _, valid_from = layer.dimension(model.valid_from_dimension)
            _, valid_to = layer.dimension(model.valid_to_dimension)
            effective_time = "CURRENT_TIMESTAMP"
            if query.snapshot and query.snapshot.mode == "as_of":
                parameters.append(query.snapshot.as_of or "")
                effective_time = f"${len(parameters)}"
            valid_from_expression = self._expression(valid_from.expression, aliases[model_name])
            valid_to_expression = self._expression(valid_to.expression, aliases[model_name])
            where.append(f"{valid_from_expression} <= {effective_time}")
            where.append(
                f"({valid_to_expression} > {effective_time} OR {valid_to_expression} IS NULL)"
            )
            explanation.append(f"slowly-changing validity enforced for {model_name}")
        sql = (
            "SELECT "
            + ", ".join(select)
            + f" FROM {_identifier(self._model(layer, base_model).table)} AS {aliases[base_model]}"
        )
        for join in joins:
            if join.relationship == "one_to_many" and not join.fanout_safe:
                raise ConfigurationError(
                    f"join {join.name!r} may fan out the fact model; split it into graph queries"
                )
            right = join.right_model if join.left_model in aliases else join.left_model
            sql += (
                f" LEFT JOIN {_identifier(self._model(layer, right).table)} AS {aliases[right]} "
                f"ON {join.sql_on}"
            )
            explanation.append(f"governed join: {join.name} ({join.relationship})")
        if where:
            sql += " WHERE " + " AND ".join(where)
        if group_by:
            sql += " GROUP BY " + ", ".join(group_by)
        if query.order_by:
            selected = {metric.name for _, metric in metrics}
            selected.update(dimension.name for _, dimension in dimensions)
            order: list[str] = []
            for metric_sort in query.order_by:
                name = self._selected_name(layer, metric_sort.field)
                if name not in selected:
                    raise ConfigurationError(f"order field {metric_sort.field!r} is not selected")
                order.append(f"{_identifier(name)} {metric_sort.direction.value.upper()}")
            sql += " ORDER BY " + ", ".join(order)
        sql += f" LIMIT {query.limit};"
        lineage = {metric.name: metric.lineage for _, metric in metrics if metric.lineage}
        lineage.update(
            {dimension.name: dimension.lineage for _, dimension in dimensions if dimension.lineage}
        )
        return CompiledSemanticQuery(
            sql=sql,
            parameters=tuple(parameters),
            semantic_layer=layer.definition.name,
            semantic_layer_version=layer.definition.version,
            semantic_layer_digest=layer.digest,
            semantic_model=base_model,
            selected_models=model_names,
            joins=tuple(join.name for join in joins),
            lineage=lineage,
            explanation=tuple(explanation),
        )

    @staticmethod
    def _validate_compilation_shape(query: MetricQuery) -> None:
        if query.time_range:
            raise ConfigurationError(
                "normalize time_range into typed filters or relative_period before compilation"
            )
        if query.comparison:
            raise ConfigurationError(
                "period comparisons require separate semantic queries and a compare_periods graph node"
            )
        if query.rolling_windows:
            raise ConfigurationError(
                "rolling windows require an explicit calculate graph node or a registered analytical method"
            )
        if query.cohort_dimensions:
            raise ConfigurationError(
                "cohort analysis requires an explicit analytical method graph node"
            )
        if query.funnel_metrics:
            raise ConfigurationError(
                "funnel analysis requires separate result sets and an explicit analytical method graph node"
            )
        unsupported_operations = {
            AnalyticalOperation.CONCENTRATION,
            AnalyticalOperation.RANK,
            AnalyticalOperation.COHORT,
            AnalyticalOperation.FUNNEL,
        }
        if query.operation in unsupported_operations:
            raise ConfigurationError(
                f"operation {query.operation.value!r} must be lowered into analytical graph nodes"
            )

    @staticmethod
    def _advanced(query: MetricQuery) -> bool:
        return bool(
            query.time_grain
            or query.relative_period
            or query.comparison
            or query.rolling_windows
            or query.snapshot
            or query.operation != AnalyticalOperation.NONE
            or query.cohort_dimensions
            or query.funnel_metrics
        )

    @staticmethod
    def _model(layer: SemanticLayer, name: str) -> SemanticModelDefinition:
        return next(item for item in layer.definition.models if item.name == name)

    @staticmethod
    def _expression(expression: str, alias: str) -> str:
        return f"{alias}.{expression}" if expression.replace("_", "").isalnum() else expression

    def _metric_expression(self, metric: MetricDefinition, alias: str) -> str:
        expression = self._expression(metric.expression, alias)
        if metric.behavior == MetricBehavior.RATIO:
            assert metric.numerator is not None and metric.denominator is not None
            numerator = self._expression(metric.numerator, alias)
            denominator = self._expression(metric.denominator, alias)
            compiled = f"SUM({numerator}) / NULLIF(SUM({denominator}), 0)"
        elif metric.behavior == MetricBehavior.NON_ADDITIVE:
            compiled = f"MAX({expression})"
        else:
            compiled = self._aggregate(metric.aggregation, expression)
        return f"COALESCE({compiled}, 0)" if metric.null_behavior == NullBehavior.ZERO else compiled

    @staticmethod
    def _time_bucket(expression: str, grain: TimeGrain) -> str:
        return f"DATE_TRUNC('{grain.value}', {expression})"

    def _join_path(
        self, layer: SemanticLayer, base: str, targets: set[str]
    ) -> list[JoinDefinition]:
        selected: list[JoinDefinition] = []
        reached = {base}
        remaining = set(targets)
        while remaining:
            candidate = next(
                (
                    join
                    for join in layer.definition.joins
                    if (join.left_model in reached and join.right_model in remaining)
                    or (join.right_model in reached and join.left_model in remaining)
                ),
                None,
            )
            if candidate is None:
                raise ConfigurationError(
                    f"no direct governed join from {sorted(reached)} to {sorted(remaining)}"
                )
            required_tags = set(candidate.authorization_tags) - self._authorization_tags
            if required_tags:
                raise ConfigurationError(
                    f"join {candidate.name!r} requires authorization tags {sorted(required_tags)}"
                )
            selected.append(candidate)
            reached.update((candidate.left_model, candidate.right_model))
            remaining -= reached
        return selected
