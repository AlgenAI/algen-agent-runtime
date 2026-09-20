from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError, NotFoundError
from algen_agent_runtime.semantics.contracts import (
    Certification,
    DimensionDefinition,
    MetricDefinition,
    MetricQuery,
    SemanticLayerDefinition,
    Sensitivity,
)

_CERTIFICATION_RANK: dict[Certification, int] = {
    "draft": 0,
    "verified": 1,
    "certified": 2,
}
_SENSITIVITY_RANK: dict[Sensitivity, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


class SemanticLayer:
    """Validated, vendor-neutral metric catalog loaded from YAML or code."""

    def __init__(self, definition: SemanticLayerDefinition) -> None:
        self.definition = definition
        self._metrics: dict[str, tuple[str, MetricDefinition]] = {}
        self._dimensions: dict[str, tuple[str, DimensionDefinition]] = {}
        for model in definition.models:
            for metric in model.metrics:
                self._index(self._metrics, metric.name, model.name, metric)
                for synonym in metric.synonyms:
                    self._index(self._metrics, synonym, model.name, metric)
            for dimension in model.dimensions:
                self._index(self._dimensions, dimension.name, model.name, dimension)
                for synonym in dimension.synonyms:
                    self._index(self._dimensions, synonym, model.name, dimension)
        rendered = json.dumps(definition.model_dump(mode="json"), sort_keys=True)
        self.digest = hashlib.sha256(rendered.encode()).hexdigest()
        self._prompt_context: str | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> SemanticLayer:
        source = Path(path)
        try:
            payload = yaml.safe_load(source.read_text(encoding="utf-8"))
            return cls(SemanticLayerDefinition.model_validate(payload))
        except (OSError, yaml.YAMLError, ValueError) as exc:
            raise ConfigurationError(f"invalid semantic layer {source}: {exc}") from exc

    @staticmethod
    def _index(index: dict[str, tuple[str, Any]], alias: str, model: str, value: Any) -> None:
        key = _key(alias)
        current = index.get(key)
        if current and current != (model, value):
            raise ConfigurationError(f"ambiguous semantic alias {alias!r}")
        index[key] = (model, value)

    def metric(self, name: str) -> tuple[str, MetricDefinition]:
        try:
            return self._metrics[_key(name)]
        except KeyError as exc:
            qualified = self._qualified_reference(self._metrics, name)
            if qualified is not None:
                return qualified
            raise NotFoundError(f"semantic metric {name!r} is not defined") from exc

    def dimension(self, name: str) -> tuple[str, DimensionDefinition]:
        try:
            return self._dimensions[_key(name)]
        except KeyError as exc:
            qualified = self._qualified_reference(self._dimensions, name)
            if qualified is not None:
                return qualified
            raise NotFoundError(f"semantic dimension {name!r} is not defined") from exc

    @staticmethod
    def _qualified_reference(
        index: dict[str, tuple[str, Any]], name: str
    ) -> tuple[str, Any] | None:
        """Resolve an unambiguous ``model.reference`` emitted by a planner."""
        if "." not in name:
            return None
        model_name, reference_name = name.rsplit(".", 1)
        resolved = index.get(_key(reference_name))
        if resolved is None or _key(resolved[0]) != _key(model_name):
            return None
        return resolved

    def metric_names(self) -> tuple[str, ...]:
        return tuple(metric.name for model in self.definition.models for metric in model.metrics)

    def dimension_names(self) -> tuple[str, ...]:
        return tuple(
            dimension.name for model in self.definition.models for dimension in model.dimensions
        )

    def validate_query(
        self,
        query: MetricQuery,
        *,
        minimum_certification: Certification = "draft",
        maximum_sensitivity: Sensitivity = "restricted",
        authorization_tags: frozenset[str] = frozenset(),
    ) -> None:
        resolved_metrics, resolved_dimensions = self.validate_selection(
            query.metrics,
            query.dimensions,
            minimum_certification=minimum_certification,
            maximum_sensitivity=maximum_sensitivity,
            authorization_tags=authorization_tags,
        )
        model_names = {model for model, _ in (*resolved_metrics, *resolved_dimensions)}
        models = {
            model.name: model for model in self.definition.models if model.name in model_names
        }
        for model in models.values():
            filtered_dimensions = {self.dimension(item.field)[1].name for item in query.filters}
            missing = set(model.required_filter_dimensions) - filtered_dimensions
            if missing:
                raise ConfigurationError(
                    f"semantic model {model.name!r} requires filters for {sorted(missing)}"
                )

    def validate_selection(
        self,
        metrics: tuple[str, ...],
        dimensions: tuple[str, ...],
        *,
        minimum_certification: Certification = "draft",
        maximum_sensitivity: Sensitivity = "restricted",
        authorization_tags: frozenset[str] = frozenset(),
    ) -> tuple[list[tuple[str, MetricDefinition]], list[tuple[str, DimensionDefinition]]]:
        """Validate selected semantic references without applying query-filter rules."""
        resolved_metrics, resolved_dimensions = self.validate_references(metrics, dimensions)
        model_names = {model for model, _ in (*resolved_metrics, *resolved_dimensions)}
        models = {
            model.name: model for model in self.definition.models if model.name in model_names
        }
        selected_dimensions = {item.name for _, item in resolved_dimensions}
        for _, metric in resolved_metrics:
            if metric.allowed_dimensions:
                unknown = selected_dimensions - set(metric.allowed_dimensions)
                if unknown:
                    raise ConfigurationError(
                        f"metric {metric.name!r} cannot be grouped by {sorted(unknown)}"
                    )
            self._validate_governance(
                "metric",
                metric.name,
                metric.certification,
                metric.sensitivity,
                metric.authorization_tags,
                minimum_certification,
                maximum_sensitivity,
                authorization_tags,
            )
        for _, dimension in resolved_dimensions:
            self._validate_access(
                "dimension",
                dimension.name,
                dimension.sensitivity,
                dimension.authorization_tags,
                maximum_sensitivity,
                authorization_tags,
            )
        for model in models.values():
            self._validate_governance(
                "model",
                model.name,
                model.certification,
                model.sensitivity,
                model.authorization_tags,
                minimum_certification,
                maximum_sensitivity,
                authorization_tags,
            )
        return resolved_metrics, resolved_dimensions

    def validate_generated_sql(
        self,
        metrics: tuple[str, ...],
        dimensions: tuple[str, ...],
        sql: str,
    ) -> None:
        """Enforce model source and filter invariants on externally generated SQL."""
        resolved_metrics, resolved_dimensions = self.validate_references(metrics, dimensions)
        model_names = {model for model, _ in (*resolved_metrics, *resolved_dimensions)}
        normalized = re.sub(r'"', "", sql.lower())
        for model in self.definition.models:
            if model.name not in model_names:
                continue
            if not re.search(rf"\b{re.escape(model.table.lower())}\b", normalized):
                raise ConfigurationError(
                    f"generated SQL for semantic model {model.name!r} must reference "
                    f"{model.table!r}"
                )
            dimensions_by_name = {item.name: item for item in model.dimensions}
            for required in model.required_filter_dimensions:
                expression = dimensions_by_name[required].expression
                if not self._has_filter_predicate(normalized, expression):
                    raise ConfigurationError(
                        f"generated SQL for semantic model {model.name!r} requires a "
                        f"predicate on {required!r}"
                    )
            for governed_filter in model.default_filters:
                dimension = dimensions_by_name[governed_filter.field]
                if governed_filter.operator != "eq" or not isinstance(governed_filter.value, bool):
                    raise ConfigurationError(
                        f"generated-SQL validation supports boolean equality defaults only; "
                        f"model {model.name!r} must use the semantic compiler for other defaults"
                    )
                if not self._has_boolean_filter(
                    normalized, dimension.expression, governed_filter.value
                ):
                    raise ConfigurationError(
                        f"generated SQL for semantic model {model.name!r} must enforce "
                        f"{governed_filter.field}={governed_filter.value}"
                    )

    def query_policy_context(
        self, metrics: tuple[str, ...], dimensions: tuple[str, ...]
    ) -> dict[str, Any]:
        """Return the bounded model policies relevant to one planned query."""
        resolved_metrics, resolved_dimensions = self.validate_references(metrics, dimensions)
        model_names = sorted({model for model, _ in (*resolved_metrics, *resolved_dimensions)})
        policies = []
        for model in self.definition.models:
            if model.name not in model_names:
                continue
            policies.append(
                {
                    "model": model.name,
                    "table": model.table,
                    "certification": model.certification,
                    "sensitivity": model.sensitivity,
                    "default_filters": [
                        item.model_dump(mode="json") for item in model.default_filters
                    ],
                    "required_filter_dimensions": model.required_filter_dimensions,
                    "freshness": (
                        model.freshness.model_dump(mode="json") if model.freshness else None
                    ),
                    "metadata": model.metadata,
                }
            )
        return {"models": policies}

    @staticmethod
    def _has_filter_predicate(sql: str, expression: str) -> bool:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", expression):
            return False
        field = rf"(?:\b[a-z_][a-z0-9_]*\.)?\b{re.escape(expression.lower())}\b"
        operator = r"(?:=|<>|!=|<=|>=|<|>|\bbetween\b|\bin\b|\bis\b)"
        return re.search(field + r"\s*" + operator, sql) is not None

    @staticmethod
    def _has_boolean_filter(sql: str, expression: str, value: bool) -> bool:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", expression):
            return False
        field = rf"(?:\b[a-z_][a-z0-9_]*\.)?\b{re.escape(expression.lower())}\b"
        literal = "true" if value else "false"
        return re.search(field + rf"\s*(?:=|\bis\b)\s*{literal}\b", sql) is not None

    @staticmethod
    def _validate_governance(
        kind: str,
        name: str,
        certification: Certification,
        sensitivity: Sensitivity,
        required_tags: tuple[str, ...],
        minimum_certification: Certification,
        maximum_sensitivity: Sensitivity,
        authorization_tags: frozenset[str],
    ) -> None:
        if _CERTIFICATION_RANK[certification] < _CERTIFICATION_RANK[minimum_certification]:
            raise ConfigurationError(
                f"semantic {kind} {name!r} is {certification}, below required "
                f"certification {minimum_certification}"
            )
        SemanticLayer._validate_access(
            kind,
            name,
            sensitivity,
            required_tags,
            maximum_sensitivity,
            authorization_tags,
        )

    @staticmethod
    def _validate_access(
        kind: str,
        name: str,
        sensitivity: Sensitivity,
        required_tags: tuple[str, ...],
        maximum_sensitivity: Sensitivity,
        authorization_tags: frozenset[str],
    ) -> None:
        if _SENSITIVITY_RANK[sensitivity] > _SENSITIVITY_RANK[maximum_sensitivity]:
            raise ConfigurationError(
                f"semantic {kind} {name!r} sensitivity {sensitivity} exceeds allowed "
                f"level {maximum_sensitivity}"
            )
        missing_tags = set(required_tags) - authorization_tags
        if missing_tags:
            raise ConfigurationError(
                f"semantic {kind} {name!r} requires authorization tags {sorted(missing_tags)}"
            )

    def validate_references(
        self, metrics: tuple[str, ...], dimensions: tuple[str, ...]
    ) -> tuple[list[tuple[str, MetricDefinition]], list[tuple[str, DimensionDefinition]]]:
        """Resolve planner references, including dimension-only raw-data queries."""
        return (
            [self.metric(name) for name in metrics],
            [self.dimension(name) for name in dimensions],
        )

    def prompt_context(self) -> str:
        """Compact governed context suitable for a planner or SQL generator."""
        cached = self._prompt_context
        if cached is not None:
            return cached
        payload = {
            "semantic_layer": self.definition.name,
            "version": self.definition.version,
            "digest": self.digest,
            "timezone": self.definition.timezone,
            "currency": self.definition.currency,
            "models": [
                {
                    "name": model.name,
                    "table": model.table,
                    "default_time_dimension": model.default_time_dimension,
                    "certification": model.certification,
                    "sensitivity": model.sensitivity,
                    "authorization_tags": model.authorization_tags,
                    "default_filters": [
                        item.model_dump(mode="json") for item in model.default_filters
                    ],
                    "required_filter_dimensions": model.required_filter_dimensions,
                    "freshness": (
                        model.freshness.model_dump(mode="json") if model.freshness else None
                    ),
                    "metadata": model.metadata,
                    "dimensions": [item.model_dump(mode="json") for item in model.dimensions],
                    "metrics": [item.model_dump(mode="json") for item in model.metrics],
                }
                for model in self.definition.models
            ],
            "joins": [item.model_dump(mode="json") for item in self.definition.joins],
        }
        self._prompt_context = json.dumps(payload, indent=2, sort_keys=True)
        return self._prompt_context


class SemanticLayerRegistry:
    def __init__(self) -> None:
        self._layers: dict[str, SemanticLayer] = {}

    def register(self, layer: SemanticLayer) -> None:
        name = layer.definition.name
        if name in self._layers:
            raise ConflictError(f"semantic layer {name!r} is already registered")
        self._layers[name] = layer

    def get(self, name: str) -> SemanticLayer:
        try:
            return self._layers[name]
        except KeyError as exc:
            raise NotFoundError(f"semantic layer {name!r} is not registered") from exc

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._layers))
