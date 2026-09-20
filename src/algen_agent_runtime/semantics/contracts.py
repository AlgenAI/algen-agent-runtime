from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Certification = Literal["draft", "verified", "certified"]
Sensitivity = Literal["public", "internal", "confidential", "restricted"]


class SemanticType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"


class Aggregation(StrEnum):
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    EXPRESSION = "expression"


class MetricBehavior(StrEnum):
    ADDITIVE = "additive"
    SEMI_ADDITIVE = "semi_additive"
    NON_ADDITIVE = "non_additive"
    RATIO = "ratio"
    DERIVED = "derived"


class NullBehavior(StrEnum):
    PRESERVE = "preserve"
    ZERO = "zero"
    EXCLUDE = "exclude"


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class SQLDialect(StrEnum):
    POSTGRES = "postgres"


class AnalyticalOperation(StrEnum):
    NONE = "none"
    CONTRIBUTION = "contribution"
    CONCENTRATION = "concentration"
    RANK = "rank"
    COHORT = "cohort"
    FUNNEL = "funnel"


class FilterOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class DimensionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    description: str
    expression: str
    type: SemanticType
    synonyms: tuple[str, ...] = ()
    is_time: bool = False
    sensitivity: Sensitivity = "internal"
    authorization_tags: tuple[str, ...] = ()
    lineage: tuple[str, ...] = ()
    calendar: str | None = None
    late_arrival_seconds: int = Field(default=0, ge=0)


class MetricDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    description: str
    expression: str
    aggregation: Aggregation
    format: str = "number"
    synonyms: tuple[str, ...] = ()
    allowed_dimensions: tuple[str, ...] = ()
    filters: tuple[str, ...] = ()
    additive: bool = True
    certification: Certification = "draft"
    owner: str | None = None
    sensitivity: Sensitivity = "internal"
    authorization_tags: tuple[str, ...] = ()
    behavior: MetricBehavior = MetricBehavior.ADDITIVE
    numerator: str | None = None
    denominator: str | None = None
    semi_additive_dimensions: tuple[str, ...] = ()
    null_behavior: NullBehavior = NullBehavior.PRESERVE
    lineage: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_behavior(self) -> MetricDefinition:
        if self.behavior == MetricBehavior.RATIO and not (self.numerator and self.denominator):
            raise ValueError("ratio metrics require numerator and denominator expressions")
        if self.behavior == MetricBehavior.SEMI_ADDITIVE and not self.semi_additive_dimensions:
            raise ValueError("semi-additive metrics require semi_additive_dimensions")
        return self


class MetricFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str
    operator: FilterOperator = FilterOperator.EQ
    value: str | int | float | bool | tuple[str | int | float | bool, ...] | None = None

    @model_validator(mode="after")
    def validate_value(self) -> MetricFilter:
        null_operator = self.operator in {
            FilterOperator.IS_NULL,
            FilterOperator.IS_NOT_NULL,
        }
        set_operator = self.operator in {FilterOperator.IN, FilterOperator.NOT_IN}
        if null_operator and self.value is not None:
            raise ValueError("null operators do not accept a value")
        if not null_operator and self.value is None:
            raise ValueError("filter operator requires a value")
        if set_operator and not isinstance(self.value, tuple):
            raise ValueError("in operators require a tuple value")
        if isinstance(self.value, tuple) and not set_operator:
            raise ValueError("tuple values are supported only by in operators")
        if set_operator and not self.value:
            raise ValueError("in operators require at least one value")
        return self


class FreshnessPolicy(BaseModel):
    """Maximum accepted source age, measured using a model dimension."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str
    max_age_seconds: int = Field(gt=0)


class SemanticModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str
    table: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.]*$")
    primary_key: tuple[str, ...]
    default_time_dimension: str | None = None
    dimensions: tuple[DimensionDefinition, ...]
    metrics: tuple[MetricDefinition, ...]
    certification: Certification = "draft"
    sensitivity: Sensitivity = "internal"
    authorization_tags: tuple[str, ...] = ()
    default_filters: tuple[MetricFilter, ...] = ()
    required_filter_dimensions: tuple[str, ...] = ()
    freshness: FreshnessPolicy | None = None
    valid_from_dimension: str | None = None
    valid_to_dimension: str | None = None
    late_arrival_seconds: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_names(self) -> SemanticModelDefinition:
        dimension_names = [item.name for item in self.dimensions]
        metric_names = [item.name for item in self.metrics]
        if len(dimension_names) != len(set(dimension_names)):
            raise ValueError(f"semantic model {self.name!r} has duplicate dimensions")
        if len(metric_names) != len(set(metric_names)):
            raise ValueError(f"semantic model {self.name!r} has duplicate metrics")
        if self.default_time_dimension and self.default_time_dimension not in dimension_names:
            raise ValueError(f"semantic model {self.name!r} default_time_dimension is not defined")
        filter_fields = [item.field for item in self.default_filters]
        unknown_filters = set(filter_fields) - set(dimension_names)
        if unknown_filters:
            raise ValueError(
                f"semantic model {self.name!r} default filters reference unknown "
                f"dimensions {sorted(unknown_filters)}"
            )
        if len(filter_fields) != len(set(filter_fields)):
            raise ValueError(f"semantic model {self.name!r} has duplicate default filters")
        unknown_required = set(self.required_filter_dimensions) - set(dimension_names)
        if unknown_required:
            raise ValueError(
                f"semantic model {self.name!r} requires unknown filter dimensions "
                f"{sorted(unknown_required)}"
            )
        if self.freshness and self.freshness.field not in dimension_names:
            raise ValueError(
                f"semantic model {self.name!r} freshness references unknown dimension "
                f"{self.freshness.field!r}"
            )
        for field in (self.valid_from_dimension, self.valid_to_dimension):
            if field and field not in dimension_names:
                raise ValueError(
                    f"semantic model {self.name!r} SCD field {field!r} is not a dimension"
                )
        return self


class JoinDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    left_model: str
    right_model: str
    relationship: Literal["one_to_one", "one_to_many", "many_to_one"]
    sql_on: str
    description: str
    fanout_safe: bool = False
    authorization_tags: tuple[str, ...] = ()


class CalendarDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    timezone: str = "UTC"
    week_start: Literal["monday", "sunday"] = "monday"
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)


class RelativePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str
    unit: TimeGrain
    count: int = Field(ge=1, le=10_000)
    include_current: bool = False


class PeriodComparison(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["previous_period", "previous_year", "same_dtd"]
    offset: int = Field(default=1, ge=1, le=100)


class RollingWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric: str
    periods: int = Field(ge=2, le=3650)
    function: Literal["sum", "avg", "min", "max"] = "avg"


class SnapshotSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str
    mode: Literal["latest", "as_of", "same_dtd"] = "latest"
    as_of: str | None = None

    @model_validator(mode="after")
    def validate_as_of(self) -> SnapshotSelection:
        if self.mode == "as_of" and not self.as_of:
            raise ValueError("as_of snapshot selection requires an as_of value")
        return self


class SemanticLayerDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    version: str
    description: str
    timezone: str = "UTC"
    currency: str | None = None
    models: tuple[SemanticModelDefinition, ...]
    joins: tuple[JoinDefinition, ...] = ()
    calendars: tuple[CalendarDefinition, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_graph(self) -> SemanticLayerDefinition:
        names = [model.name for model in self.models]
        if len(names) != len(set(names)):
            raise ValueError("semantic layer has duplicate model names")
        known = set(names)
        known_dimensions = {
            dimension.name for model in self.models for dimension in model.dimensions
        }
        for model in self.models:
            for metric in model.metrics:
                unknown = set(metric.allowed_dimensions) - known_dimensions
                if unknown:
                    raise ValueError(
                        f"metric {metric.name!r} references unknown dimensions {sorted(unknown)}"
                    )
        join_names: set[str] = set()
        for join in self.joins:
            if join.name in join_names:
                raise ValueError(f"semantic layer has duplicate join {join.name!r}")
            join_names.add(join.name)
            if join.left_model not in known or join.right_model not in known:
                raise ValueError(f"join {join.name!r} references an unknown model")
        return self


class MetricSort(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    field: str
    direction: SortDirection = SortDirection.ASC


class MetricQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metrics: tuple[str, ...] = Field(min_length=1)
    dimensions: tuple[str, ...] = ()
    time_range: str | None = None
    filters: tuple[MetricFilter, ...] = ()
    order_by: tuple[MetricSort, ...] = ()
    limit: int = Field(default=500, ge=1, le=10_000)
    time_grain: TimeGrain | None = None
    relative_period: RelativePeriod | None = None
    comparison: PeriodComparison | None = None
    rolling_windows: tuple[RollingWindow, ...] = ()
    snapshot: SnapshotSelection | None = None
    operation: AnalyticalOperation = AnalyticalOperation.NONE
    operation_metric: str | None = None
    cohort_dimensions: tuple[str, ...] = ()
    funnel_metrics: tuple[str, ...] = ()


class CompiledSemanticQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    sql: str
    parameters: tuple[str | int | float | bool, ...] = ()
    semantic_layer: str
    semantic_layer_version: str
    semantic_layer_digest: str
    semantic_model: str
    dialect: SQLDialect = SQLDialect.POSTGRES
    selected_models: tuple[str, ...] = ()
    joins: tuple[str, ...] = ()
    lineage: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    explanation: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class AnalysisKind(StrEnum):
    DESCRIPTIVE = "descriptive"
    DIAGNOSTIC = "diagnostic"
    ANOMALY = "anomaly"
    FORECAST = "forecast"
    STATIC_SCENARIO = "static_scenario"
    CAUSAL_SCENARIO = "causal_scenario"
    OPTIMIZATION = "optimization"
    DISCOVERY = "discovery"


class AnalysisStatus(StrEnum):
    COMPLETED = "completed"
    INSUFFICIENT_DATA = "insufficient_data"
    REQUIRES_MODEL = "requires_model"
    REQUIRES_CLARIFICATION = "requires_clarification"
    FAILED = "failed"


class Assumption(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    statement: str
    material: bool = True
    user_confirmed: bool = False


class DataRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    reason: str
    available: bool = False
    source: str | None = None


class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: AnalysisKind
    status: AnalysisStatus
    summary: str
    values: dict[str, Any] = Field(default_factory=dict)
    assumptions: tuple[Assumption, ...] = ()
    requirements: tuple[DataRequirement, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_interval: tuple[float, float] | None = None
    method: str | None = None
    model_version: str | None = None
    warnings: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
