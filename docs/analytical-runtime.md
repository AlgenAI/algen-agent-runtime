# Analytical Runtime

Algen Agent Runtime includes provider-neutral building blocks for governed analytical agents. The core
contains no airline, finance, support, or other domain formulas. Applications register semantic
definitions, deterministic methods, remote analytical models, and node handlers through dependency
injection.

## Typed analytical graphs

`AnalyticalGraph` is a validated DAG of `semantic_query`, `join_results`, `compare_periods`,
`calculate`, `model_call`, `rank`, `verify`, and `compose` nodes. Every node declares dependencies,
JSON input/output schemas, retry and cache policy, timeout/row/byte/cost budgets, and an optional
concurrency key. Cycles, missing dependencies, and undeclared input references fail validation.

`AnalyticalGraphEngine` schedules independent nodes concurrently and saves state before and after
each node. Results are keyed by node ID and never implicitly flattened. Each `AnalyticalResult`
carries source/column lineage, upstream handles, semantic and model/method versions, freshness, and
the query fingerprint. Deployment-level concurrency and timeout settings are enforced as upper
bounds even when an application submits a looser graph. Use `InMemoryAnalyticalGraphStore` for tests and
`PostgresAnalyticalGraphStore` for restart-safe checkpoints.

```python
from algen_agent_runtime.analytics import (
    AnalyticalGraph, AnalyticalNode, GraphExecutionContext, ResultReference,
)

graph = AnalyticalGraph(
    name="period-comparison",
    version="1.0.0",
    nodes=(
        AnalyticalNode(id="current", kind="semantic_query"),
        AnalyticalNode(id="baseline", kind="semantic_query"),
        AnalyticalNode(
            id="change",
            kind="compare_periods",
            dependencies=("current", "baseline"),
            inputs={
                "current": ResultReference(node_id="current"),
                "baseline": ResultReference(node_id="baseline"),
            },
            configuration={"keys": ["route"], "metric": "revenue"},
        ),
    ),
)
state = await container.analytical_graphs.run(
    graph, GraphExecutionContext(tenant_id="tenant-a", user_id="user-a")
)
```

Built-in handlers implement relationship-checked row joins, period deltas, allowlisted arithmetic,
ranking, structural verification, and composition. Applications register semantic-query, LLM, and
model-call handlers because those require deployment-specific providers and policies.

The container-owned engine exposes `register_handler(kind, handler)` for those application adapters.
This keeps concurrency bounds, checkpoints, schemas, retries, result budgets, caching, and lineage in
Runtime while leaving database drivers and domain policy in the application. Application query
credentials can be declared without embedding a secret:

```yaml
query_sources:
  analytics:
    type: postgres
    connection_url: env://ANALYTICS_QUERY_DSN
    read_only: true
    purpose: executive_analytics
    authorization_tags: [analytics.read]
```

`query_sources` describes an application-owned governed backend; Runtime does not infer a schema or
execute arbitrary SQL from this configuration. Register a `semantic_query` handler that applies the
semantic compiler, query governance, and the deployment's read-only executor.

`SemanticQueryGraphPlanner` lowers one or more `SemanticQueryTask` definitions into independently
executable query nodes using `ProductionSemanticQueryCompiler`, then validates all application-owned
comparison, calculation, ranking, method, verification, and composition dependencies as one DAG.
Unsafe fan-out, multi-fact aggregation, implicit period comparison, rolling/cohort/funnel operations,
and semi-additive aggregation are never silently approximated: they must be represented explicitly.

## Analytical methods and model services

`AnalyticalMethodRegistry` accepts a versioned `AnalyticalMethodManifest` plus an implementation. A
manifest declares analysis kinds, JSON schemas, required metrics/dimensions/history/sample size/model
artifacts, uncertainty behavior, lifecycle, validation/backtest references, approval, and deprecation.
The registry validates those requirements before calling application code.

`ModelServiceRegistry` is deliberately separate from LLM routing. It discovers and pins forecast,
optimization, simulation, and causal services; validates batches; checks health and drift state;
applies timeouts/cancellation; validates outputs; and walks an explicit fallback chain. Implement the
`ModelService` protocol locally or use `HTTPModelService` with an allowlisted public endpoint. Remote
responses retain model and feature lineage and may carry confidence intervals.

## Query governance and verification

`QueryGovernanceEngine` enforces purpose, authorization tags, denied columns, metric allowlists,
source certification, freshness/completeness, row/scan/compute quotas, and SQL/download/export policy.
`GovernedQueryExecutor` adds query estimation, per-tenant/purpose concurrency, authorization-aware
fingerprints, scoped caching, timeout, and an audit callback around any query backend. Database RLS,
grants, and read-only credentials remain the final security boundary.

`AnalyticalVerifier` checks claim-to-cell references, numerical equality, metric certification,
minimum sample size, comparison-window equality, and required model versions. `EvaluationRunner`
turns golden questions into repeatable checks for graph node kinds, required/forbidden SQL patterns,
fixed-data numerical tolerances, and allowed/forbidden claims. Promotion requires every case to pass
and the configured aggregate score threshold.

## Distributed execution

`WorkQueue` provides idempotent enqueue, lease-based claim, renewal, completion, failure, retry, and
stale-lease recovery. `PostgresWorkQueue` uses `FOR UPDATE SKIP LOCKED`; `DistributedWorker` executes
registered work kinds with traced outcomes. PostgreSQL analytical graph checkpoints allow another
worker to resume only incomplete nodes. PostgreSQL run and conversation subscriptions poll the
durable event log, so SSE consumers see events emitted by another process.

The application still owns process supervision, worker count, deployment rollout, and registration
of handlers. Use Redis for shared caches and PostgreSQL for graphs and work queues in a horizontally
scaled deployment.

```yaml
analytical_execution:
  graph_store: postgres
  maximum_concurrency: 8
  default_node_timeout_seconds: 30
  default_graph_timeout_seconds: 300
distributed_execution:
  enabled: true
  queue_backend: postgres
  lease_seconds: 30
  worker_concurrency: 4
query_governance:
  allowed_purposes: [executive_analytics]
  require_certified_sources: true
  maximum_rows: 10000
  maximum_bytes_scanned: 1000000000
  maximum_compute_seconds: 30
```

Every graph, node, method, model-service, governance, cache, and evaluation operation emits
OpenTelemetry spans or metrics. When Traccia is enabled, the existing adapter exports these signals
under the current conversation/run identity without sending query text, result values, or cache keys.
