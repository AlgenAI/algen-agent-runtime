# Semantic metrics layer

Algen Agent Runtime provides vendor-neutral contracts for governed metrics; applications own their domain
definitions. A semantic layer gives agents stable business names and constrains planning without
embedding airline, retail, finance, or other business logic in the runtime.

## Developer workflow

1. Define models, dimensions, metrics, safe joins, ownership, certification, sensitivity, mandatory filters, and freshness in YAML.
2. Load and validate the file at application startup.
3. Register it with the Runtime container.
4. Pass `prompt_context()` to the agents that resolve intent or generate queries.
5. Record the semantic layer name, version, and digest with every analytical response and evaluation.

```python
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.semantics import (
    FilterOperator,
    MetricFilter,
    MetricQuery,
    SemanticLayer,
    ProductionSemanticQueryCompiler,
    SemanticAnalysisPlan,
    SemanticQueryGraphPlanner,
    SemanticQueryTask,
    RelativePeriod,
    TimeGrain,
)

container = build_container(settings)
sales = SemanticLayer.from_yaml("semantic_layer.yaml")
container.semantics.register(sales)

query = MetricQuery(
    metrics=("revenue",),
    dimensions=("route",),
    filters=(
        MetricFilter(
            field="departure_date",
            operator=FilterOperator.GTE,
            value="2026-09-01",
        ),
    ),
    time_grain=TimeGrain.WEEK,
    relative_period=RelativePeriod(field="departure_date", unit="day", count=30),
)

compiled = ProductionSemanticQueryCompiler(
    minimum_certification="verified",
    maximum_sensitivity="confidential",
    authorization_tags=frozenset({"commercial.read"}),
).compile(sales, query)
# SQL, parameters, selected models/joins, lineage, explanation, and digest are auditable.

# For several independent result sets, compile a validated execution graph instead.
graph = SemanticQueryGraphPlanner(ProductionSemanticQueryCompiler()).plan(
    sales,
    SemanticAnalysisPlan(
        name="commercial-analysis",
        version="1.0.0",
        queries=(SemanticQueryTask(id="current", query=query),),
    ),
)
```

Registered definitions can be inspected using `GET /v1/semantic-layers` and
`GET /v1/semantic-layers/{name}`. These endpoints require the `agents:read` scope.

## YAML example

```yaml
name: commercial_analytics
version: 2.1.0
description: Governed commercial metrics
timezone: Asia/Kolkata
currency: INR
models:
  - name: ticket_sales
    description: Sold passenger segments
    table: analytics.ticket_sales
    primary_key: [segment_id]
    default_time_dimension: departure_date
    certification: verified
    sensitivity: confidential
    authorization_tags: [commercial.read]
    required_filter_dimensions: [departure_date]
    default_filters:
      - field: is_current
        operator: eq
        value: true
    freshness:
      field: synced_at
      max_age_seconds: 86400
    dimensions:
      - name: route
        label: Route
        description: Origin-destination market
        expression: route_code
        type: string
        synonyms: [sector, market]
      - name: departure_date
        label: Departure date
        description: Scheduled local departure date
        expression: departure_date
        type: date
        is_time: true
      - name: is_current
        label: Current source row
        description: Version-selection flag applied to every query
        expression: is_current
        type: boolean
      - name: synced_at
        label: Source synchronization time
        description: Timestamp used for the freshness gate
        expression: synced_at
        type: datetime
        is_time: true
    metrics:
      - name: revenue
        label: Ticket revenue
        description: Recognized passenger revenue
        expression: revenue_amount
        aggregation: sum
        format: currency
        synonyms: [sales]
        allowed_dimensions: [route, departure_date]
        certification: certified
        owner: Finance
        behavior: additive
        null_behavior: zero
        lineage: [booking_segment.revenue_amount]
      - name: average_fare
        label: Average fare
        description: Ticket revenue divided by booked seats
        expression: revenue_amount
        aggregation: expression
        behavior: ratio
        numerator: revenue_amount
        denominator: booked_seats
        allowed_dimensions: [route, departure_date]
        certification: certified
```

Unknown fields, duplicate names, invalid model joins, unknown allowed dimensions, and ambiguous
synonyms fail startup. `MetricQuery` validates metric and dimension references before an application
turns the request into executable SQL. The canonical SHA-256 digest changes whenever the validated
definition changes, making runs and evaluations reproducible.

`default_filters` are protected invariants. A caller cannot override them, and the compiler adds them
as parameters. `required_filter_dimensions` prevent unbounded or semantically ambiguous point-in-time
queries. `freshness` adds a source-age predicate. Trusted application code selects the minimum
certification, maximum sensitivity, and authorization tags accepted for a query. When an LLM produces
SQL outside the compiler, call `validate_generated_sql(metrics, dimensions, sql)` after the ordinary
read-only SQL validator to enforce source-table, default-filter, and required-filter invariants.

## Compiler choices

`SemanticQueryCompiler` remains the compact, backwards-compatible one-model compiler.
`ProductionSemanticQueryCompiler` adds governed many-to-one/one-to-one joins with fan-out rejection,
ratio/non-additive/null behavior, semi-additive safety checks, time grains, relative periods,
freshness, slowly-changing validity, snapshot selection, contribution, lineage, and explainable
compilation metadata. `SemanticQueryGraphPlanner` compiles independent governed queries into
separately addressable `semantic_query` nodes and validates their explicit downstream transforms.
Multi-fact metrics and same-DTD comparisons are
deliberately split into separate `AnalyticalGraph` result handles and joined or compared by an explicit
node; this prevents silent double counting.

Typed contracts also represent calendars, comparisons, rolling windows, cohort/funnel operations,
semi-additive dimensions, late arrivals, and slowly changing validity fields. Operations that cannot
be expressed safely as one PostgreSQL `SELECT` are rejected with an actionable error; a trusted
application planner must lower them into explicit graph nodes or a registered analytical method.

## Boundary of responsibility

The semantic layer is
not the final authorization system or metrics warehouse: semantic sensitivity and tag checks are an
application gate, while database grants/RLS remain the security boundary. Publish only definitions a
user may see. Runtime prompt context must not be treated as the security boundary.

For larger deployments, generate this YAML from an existing dbt Semantic Layer, Cube, LookML, or
enterprise catalog adapter rather than maintaining duplicate definitions. Keep a stable Runtime
contract while the adapter handles the vendor-specific source format.
