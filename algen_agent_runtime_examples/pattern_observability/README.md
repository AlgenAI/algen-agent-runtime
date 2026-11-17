# Observability: metrics, dashboards and alerts

The Runtime's run-level OpenTelemetry metrics (since 0.2.0b5), recorded while a stock assistant
serves requests through the HTTP API, then exported to an OpenTelemetry Collector, scraped by
Prometheus with the shipped alert rules, and charted in Grafana with the shipped dashboards. It
runs on the mock provider: no model, key or network is needed.

```bash
# Offline: one round of traffic, metrics read back from an in-memory reader
python -m algen_agent_runtime_examples.pattern_observability.app

# With the collector, Prometheus and Grafana (docker or podman; from the repository root)
docker compose -f algen_agent_runtime_examples/pattern_observability/deploy/compose.yaml up -d
python -m algen_agent_runtime_examples.pattern_observability.app \
  --otlp-endpoint http://127.0.0.1:4318/v1/metrics --rounds 0     # until Ctrl-C
# Grafana: http://127.0.0.1:3000 (Dashboards, folder "Algen Agent Runtime")
# Prometheus alerts: http://127.0.0.1:9090/alerts
docker compose -f algen_agent_runtime_examples/pattern_observability/deploy/compose.yaml down
```

`podman compose` works the same. The stack needs the repository checkout: it mounts
`deploy/observability` (the dashboards and `alerts.yml`, also in the sdist), which the wheel does
not install. Set `OTLP_PORT`, `PROMETHEUS_PORT` or `GRAFANA_PORT` when the default host ports are
taken. `--otlp-endpoint` needs the `otlp` extra (`pip install -e '.[dev]'` has it).

| What | API | Where you see it |
| --- | --- | --- |
| Metrics without an exporter of your own | `telemetry.otlp_metrics_endpoint` (here `--otlp-endpoint`), `otlp_metrics_export_interval_seconds: 5` in `agent.yaml`; the API's lifespan installs the OTLP meter provider and flushes it on shutdown | The collector's `:8889/metrics`, then Prometheus |
| Your own meter provider | `metrics.set_meter_provider(MeterProvider(...))` before the container is built (the offline run, with an `InMemoryMetricReader`) | The series printed at the end |
| Run outcomes | `runs.started`, `runs.finished` (`status`, `reason`), `run.duration`, `runs.active`, `run.pauses` | Overview dashboard: runs by status and reason, duration, executing and paused |
| Approvals | `approvals.requested`, `approvals.resolved` (`outcome`), `approval.wait.duration` | Overview dashboard: approvals and approval wait |
| Model and tool calls | `model.calls`, `model.tokens`, `tool.calls` (`outcome` `completed`, `cancelled`, ...), `policy.denials` | Models and tools dashboard |
| HTTP API and the run-admission SLO | `api.requests` by route template and status (`403` for the request without `runs:write`) | Overview dashboard: requests, admission latency and errors against the SLO |
| Alert rules | `deploy/observability/prometheus/alerts.yml`, loaded by `deploy/prometheus.yml` | Prometheus, Alerts (17 rules; none fire on this traffic) |
| Cancellation | `DELETE /v1/runs/{id}` while the async `stock.audit` tool waits: the handler is cancelled where it waits | `runs_finished_total{status="cancelled"}`, `tool_calls_total{outcome="cancelled"}` |

```text
deploy/compose.yaml                the collector, Prometheus and Grafana, names prefixed algen-rt-ex-
deploy/otel-collector.yaml         OTLP/HTTP in, Prometheus exporter out (default name translation)
deploy/prometheus.yml              scrapes the collector (honor_labels keeps job = service.name), loads alerts.yml
deploy/grafana/*.yaml              provisions the Prometheus data source and the shipped dashboards
tools.py, plugin.py, agent.yaml    the stock tools, StockPlugin, the agent and its telemetry settings
app.py                             one round of traffic through the in-process HTTP API, and the readout
```

Part of the offline output (counts per round):

```text
== Round 1 ==
1. lookup           -> completed
2. reorder approved  -> awaiting_approval, decision 202, completed
3. reorder rejected  -> awaiting_approval, decision 202, failed
4. injection        -> failed (the input policy denied it)
5. hand count       -> DELETE 200, cancelled while stock.audit ran
6. without runs:write -> 403

algen_agent_runtime_runs_finished_total
  {agent="stock-assistant", reason="PolicyDeniedError", run_kind="run", status="failed"} 1
  {agent="stock-assistant", reason="approval_rejected", run_kind="run", status="failed"} 1
  {agent="stock-assistant", reason="error", run_kind="run", status="cancelled"} 1
  {agent="stock-assistant", reason="none", run_kind="run", status="completed"} 2
algen_agent_runtime_approvals_resolved_total
  {approval_kind="tool", outcome="approved"} 1
  {approval_kind="tool", outcome="rejected"} 1
algen_agent_runtime_tool_calls_total
  {outcome="cancelled", tool="stock.audit"} 1
  {outcome="completed", tool="stock.level"} 1
  {outcome="completed", tool="stock.reorder"} 1
algen_agent_runtime_api_requests_total
  {method="POST", route="/v1/runs", status_code="202"} 5
  {method="POST", route="/v1/runs", status_code="403"} 1
...
```

Notes:

- **`job` is `service.name`.** The dashboards' `job` variable and every alert group by it. The
  OTLP meter provider takes `service.name` from the standard `OTEL_SERVICE_NAME` (the app sets
  it to `stock-assistant` when it is unset); set it, or `OTEL_RESOURCE_ATTRIBUTES`, for your
  service, or install your own `MeterProvider` with a `Resource`.
- **No ids or content in metrics.** Attributes are agent, tool, provider, model, policy and route
  names, statuses and outcomes, never a tenant, user, run or conversation id, and never text. See
  [Metrics reference](../../docs/operations.md#metrics-reference).
- **Embedded hosts** (no HTTP API) get the same run, model, tool, approval and store metrics;
  only `api.*` needs the API app. Install the meter provider, or set
  `telemetry.otlp_metrics_endpoint`, before building the container.
- **SLOs, alert routing and runbooks** are in
  [SLOs, alerts and runbooks](../../docs/operations.md#slos-alerts-and-runbooks-single-node).
  Route `severity="page"` to whoever is on call, `ticket` to a queue and `security` to whoever
  owns authentication; rules marked `tune` have traffic-dependent thresholds.
- The images are pinned (`otel/opentelemetry-collector-contrib:0.115.1`, `prom/prometheus:v3.0.1`,
  `grafana/grafana:11.3.1`) and bind to `127.0.0.1` only. Grafana allows anonymous admin access:
  a local demo, not a deployment.
