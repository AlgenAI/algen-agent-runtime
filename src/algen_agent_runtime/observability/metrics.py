"""The OpenTelemetry metrics the Runtime records, and the catalogue that names them.

``RUNTIME_METRICS`` lists every instrument the Runtime creates: its name, instrument type, unit,
attributes and meter (instrumentation scope). docs/operations.md documents the same table, and the
dashboards and alert rules in ``deploy/observability`` are built on these names; tests keep the
three in step. ``prometheus_name`` gives the name a metric has once an OTLP pipeline writes it to
Prometheus (``algen_agent_runtime.run.duration`` in seconds becomes
``algen_agent_runtime_run_duration_seconds``).

Attributes are low-cardinality on purpose: agent, tool, provider, model, policy, workflow and
handler names (bounded by what the deployment registers), statuses, outcomes and reasons from a
fixed set. Never a tenant, user, run, session or conversation id, and never content.

Instruments come from the process-global meter provider when the object that records them is
built; without an installed SDK provider (``telemetry.otlp_metrics_endpoint``, Traccia's metrics or
the application's own) every recording is a no-op.
"""

from __future__ import annotations

import time
import weakref
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from opentelemetry import metrics

from algen_agent_runtime.observability.safe_logging import runtime_logger

_logger = runtime_logger("algen_agent_runtime.observability.metrics")

InstrumentKind = Literal["counter", "histogram", "gauge"]

# runs.finished's reason for a run whose end event names none.
_DEFAULT_END_REASON = {"completed": "none", "cancelled": "cancelled"}

#: Bucket boundaries, in seconds, for the Runtime's duration histograms: from a 5 ms tool call or
#: API answer to a run that waited an hour for its approval.
DURATION_BUCKETS_SECONDS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    900.0,
    3600.0,
)

#: The kinds of pause ``algen_agent_runtime.runs.paused`` and ``run.pauses`` report.
PAUSE_KINDS: tuple[str, ...] = (
    "approval",
    "continuation",
    "clarification",
    "input",
    "reconciliation",
    "credentials",
)


@dataclass(frozen=True)
class MetricSpec:
    """One instrument the Runtime records."""

    name: str
    instrument: InstrumentKind
    unit: str
    attributes: tuple[str, ...]
    description: str
    meter: str


def _spec(
    name: str,
    instrument: InstrumentKind,
    unit: str,
    attributes: tuple[str, ...],
    description: str,
    meter: str,
) -> MetricSpec:
    return MetricSpec(
        f"algen_agent_runtime.{name}", instrument, unit, attributes, description, meter
    )


_RUNTIME = "algen_agent_runtime.runtime"
_MODELS = "algen_agent_runtime.models"
_TOOLS = "algen_agent_runtime.tools"
_API = "algen_agent_runtime.api"
_PERSISTENCE = "algen_agent_runtime.persistence"
_EVENTS = "algen_agent_runtime.events"

#: Every instrument the Runtime records (0.2.0b5 added the run, model, tool, approval, API, store,
#: event, conversation and workflow metrics; the others were recorded before).
RUNTIME_METRICS: tuple[MetricSpec, ...] = (
    # Runs
    _spec(
        "runs.started",
        "counter",
        "{run}",
        ("agent", "run_kind"),
        "Runs started",
        _RUNTIME,
    ),
    _spec(
        "runs.finished",
        "counter",
        "{run}",
        ("agent", "run_kind", "status", "reason"),
        "Runs that ended, by final status and reason",
        _RUNTIME,
    ),
    _spec(
        "run.duration",
        "histogram",
        "s",
        ("agent", "run_kind", "status"),
        "Time from a run's start to its end, pauses included",
        _RUNTIME,
    ),
    _spec(
        "runs.active",
        "gauge",
        "{run}",
        (),
        "Runs executing in this process now",
        _RUNTIME,
    ),
    _spec(
        "runs.paused",
        "gauge",
        "{run}",
        ("pause_kind",),
        "Runs paused now, by what they wait for (runs this process paused or recovered)",
        _RUNTIME,
    ),
    _spec(
        "run.pauses",
        "counter",
        "{pause}",
        ("pause_kind",),
        "Times a run paused, by what it waits for",
        _RUNTIME,
    ),
    _spec(
        "run.resumes",
        "counter",
        "{resume}",
        ("pause_kind",),
        "Paused runs that continued, by what they waited for",
        _RUNTIME,
    ),
    _spec(
        "run.recoveries",
        "counter",
        "{run}",
        ("trigger", "outcome"),
        "Incomplete runs found after a restart or a store outage, by what happened to them",
        _RUNTIME,
    ),
    # Approvals
    _spec(
        "approvals.requested",
        "counter",
        "{approval}",
        ("approval_kind",),
        "Approval requests a run or a workflow approval node paused for",
        _RUNTIME,
    ),
    _spec(
        "approvals.resolved",
        "counter",
        "{approval}",
        ("approval_kind", "outcome"),
        "Approval requests decided or expired",
        _RUNTIME,
    ),
    _spec(
        "approval.wait.duration",
        "histogram",
        "s",
        ("approval_kind", "outcome"),
        "Time from an approval request to its decision or expiry",
        _RUNTIME,
    ),
    # Models
    _spec(
        "model.calls",
        "counter",
        "{call}",
        ("provider", "model", "purpose", "outcome", "error_kind"),
        "Calls to a model provider (each candidate and each retry is a call)",
        _MODELS,
    ),
    _spec(
        "model.call.duration",
        "histogram",
        "s",
        ("provider", "model", "purpose", "outcome"),
        "Model provider call latency, to the last streamed event",
        _MODELS,
    ),
    _spec(
        "model.tokens",
        "counter",
        "{token}",
        ("provider", "model", "purpose", "token_type"),
        "Tokens the provider reported, input and output",
        _MODELS,
    ),
    _spec(
        "model.tool_calls_invalid",
        "counter",
        "1",
        ("provider", "model", "cut_off"),
        "Model tool calls whose arguments were not a JSON object, by model",
        _MODELS,
    ),
    # Tools
    _spec(
        "tool.calls",
        "counter",
        "{call}",
        ("tool", "outcome"),
        "Tool calls through the tool executor, by outcome",
        _TOOLS,
    ),
    _spec(
        "tool.call.duration",
        "histogram",
        "s",
        ("tool", "outcome"),
        "Tool call latency through the tool executor (policies, ledger, retries included)",
        _TOOLS,
    ),
    _spec(
        "tool.refusals",
        "counter",
        "{call}",
        ("tool", "status"),
        "Tool calls a guard held back (refused, repeated or over_limit) before they ran",
        _RUNTIME,
    ),
    # Policies
    _spec(
        "policy.denials",
        "counter",
        "{decision}",
        ("boundary", "policy"),
        "Policy decisions that denied, by boundary and the policy that denied",
        "algen_agent_runtime.policies",
    ),
    _spec(
        "policy.errors",
        "counter",
        "1",
        ("boundary", "policy", "fail_mode", "error_type"),
        "Policy evaluations that raised or timed out, by boundary and fail mode",
        "algen_agent_runtime.policies",
    ),
    # HTTP API
    _spec(
        "api.requests",
        "counter",
        "{request}",
        ("method", "route", "status_code"),
        "HTTP API requests answered, by route template and status code",
        _API,
    ),
    _spec(
        "api.request.duration",
        "histogram",
        "s",
        ("method", "route", "status_code"),
        "HTTP API time to the response's first byte (an event stream's start)",
        _API,
    ),
    _spec(
        "api.admission.rejections",
        "counter",
        "{request}",
        ("route_class", "reason"),
        "Requests refused with 429 by admission limits or the event-stream limit",
        _API,
    ),
    _spec(
        "auth.attempts",
        "counter",
        "1",
        ("method", "outcome", "reason"),
        "API authentication attempts by method, outcome and failure reason",
        "algen_agent_runtime.auth",
    ),
    # Stores and events
    _spec(
        "store.unavailable",
        "counter",
        "{error}",
        ("store",),
        "Store calls that failed because PostgreSQL or Redis was unavailable",
        _PERSISTENCE,
    ),
    _spec(
        "postgres.pool.connections",
        "gauge",
        "{connection}",
        ("state",),
        "PostgreSQL pool connections in use and idle",
        _PERSISTENCE,
    ),
    _spec(
        "postgres.pool.max_size",
        "gauge",
        "{connection}",
        (),
        "PostgreSQL pool maximum size",
        _PERSISTENCE,
    ),
    _spec(
        "events.backpressure",
        "counter",
        "{event}",
        ("kind",),
        "Run events dropped at a run's event limit, and subscribers that fell behind",
        _EVENTS,
    ),
    # Conversations and workflows
    _spec(
        "conversation.turns",
        "counter",
        "{turn}",
        ("handler", "outcome"),
        "Conversation turns answered, by outcome",
        "algen_agent_runtime.conversations",
    ),
    _spec(
        "conversation.turn.duration",
        "histogram",
        "s",
        ("handler", "outcome"),
        "Time a conversation turn took to answer or pause",
        "algen_agent_runtime.conversations",
    ),
    _spec(
        "workflow.runs.started",
        "counter",
        "{workflow}",
        ("workflow",),
        "Workflow runs started",
        "algen_agent_runtime.workflows",
    ),
    _spec(
        "workflow.runs.finished",
        "counter",
        "{workflow}",
        ("workflow", "status"),
        "Workflow runs that ended, by status",
        "algen_agent_runtime.workflows",
    ),
    _spec(
        "workflow.run.duration",
        "histogram",
        "s",
        ("workflow", "status"),
        "Time from a workflow run's creation to its end, pauses included",
        "algen_agent_runtime.workflows",
    ),
    # Recorded before 0.2.0b5
    _spec(
        "cache.operations",
        "counter",
        "",
        ("cache.backend", "cache.policy", "cache.namespace", "cache.scope", "cache.outcome"),
        "Cache gets and sets by outcome (hit, miss, stored, bypass, oversize, error)",
        "algen_agent_runtime.cache",
    ),
    _spec(
        "cache.operation.duration",
        "histogram",
        "ms",
        ("cache.backend", "cache.policy", "cache.namespace", "cache.scope"),
        "Cache operation latency",
        "algen_agent_runtime.cache",
    ),
    _spec(
        "governance.decisions",
        "counter",
        "",
        ("governance.decision", "governance.reason_code", "governance.purpose"),
        "Analytical query governance decisions",
        "algen_agent_runtime.governance",
    ),
    _spec(
        "analytics.node.executions",
        "counter",
        "",
        ("analytics.node.id", "analytics.node.kind", "outcome"),
        "Analytical graph node executions",
        "algen_agent_runtime.analytics",
    ),
    _spec(
        "analytics.node.duration",
        "histogram",
        "ms",
        ("analytics.node.id", "analytics.node.kind"),
        "Analytical graph node latency",
        "algen_agent_runtime.analytics",
    ),
    _spec(
        "method.executions",
        "counter",
        "",
        ("analytics.method.name", "analytics.method.version", "analytics.method.kind", "outcome"),
        "Analytical method executions",
        "algen_agent_runtime.methods",
    ),
    _spec(
        "model_service.calls",
        "counter",
        "",
        ("model_service.name", "model_service.version", "model_service.kind", "outcome"),
        "Model-service calls",
        "algen_agent_runtime.model_services",
    ),
    _spec(
        "work.executions",
        "counter",
        "",
        ("work.kind", "outcome"),
        "Distributed work items a worker executed",
        "algen_agent_runtime.distributed",
    ),
    _spec(
        "evaluation.cases",
        "counter",
        "",
        ("outcome",),
        "Evaluation cases run",
        "algen_agent_runtime.evaluation",
    ),
)

_SPECS: dict[str, MetricSpec] = {spec.name: spec for spec in RUNTIME_METRICS}
# Each instrument by name, with the meter provider it came from.
_INSTRUMENTS: dict[str, tuple[Any, Any]] = {}

_UNIT_SUFFIXES = {"s": "seconds", "ms": "milliseconds", "By": "bytes"}


def metric_spec(name: str) -> MetricSpec:
    """The catalogue entry for ``name`` (``KeyError`` when the Runtime records no such metric)."""
    return _SPECS[name]


def prometheus_name(spec: MetricSpec | str) -> str:
    """The metric's name in Prometheus after an OTLP pipeline (the OpenTelemetry Collector's
    Prometheus exporters, or Prometheus's own OTLP receiver) translates it.

    Dots become underscores, the unit is appended (``s`` as ``_seconds``; annotations such as
    ``{run}`` and the unit ``1`` are dropped) and counters end in ``_total``. A histogram's series
    are this name with ``_bucket``, ``_sum`` and ``_count``.
    """
    if isinstance(spec, str):
        spec = metric_spec(spec)
    name = spec.name.replace(".", "_")
    suffix = _UNIT_SUFFIXES.get(spec.unit)
    if suffix and not name.endswith(f"_{suffix}"):
        name = f"{name}_{suffix}"
    if spec.instrument == "counter":
        name = f"{name}_total"
    return name


class _NoopInstrument:
    def add(self, amount: float, attributes: Mapping[str, Any] | None = None) -> None:
        return None

    def record(self, amount: float, attributes: Mapping[str, Any] | None = None) -> None:
        return None


def instrument(name: str, callbacks: Iterable[Callable[[Any], Iterable[Any]]] | None = None) -> Any:
    """The instrument for catalogue entry ``name`` from the process-global meter provider.

    Each instrument is created once per provider and reused: before a provider is installed the
    OpenTelemetry API keeps every proxy meter it hands out, so creating one per call would grow
    without bound. A proxy instrument records into the provider installed later. A gauge's
    ``callbacks`` are registered when it is first created. Never raises: a meter that fails gives
    a no-op.
    """
    spec = _SPECS[name]
    try:
        provider = metrics.get_meter_provider()
        cached = _INSTRUMENTS.get(name)
        if cached is not None and cached[0] is provider:
            return cached[1]
        meter = provider.get_meter(spec.meter)
        created: Any
        if spec.instrument == "counter":
            created = meter.create_counter(spec.name, unit=spec.unit, description=spec.description)
        elif spec.instrument == "histogram":
            advisory = DURATION_BUCKETS_SECONDS if spec.unit == "s" else None
            created = meter.create_histogram(
                spec.name,
                unit=spec.unit,
                description=spec.description,
                explicit_bucket_boundaries_advisory=advisory,
            )
        else:
            created = meter.create_observable_gauge(
                spec.name,
                callbacks=list(callbacks or ()),
                unit=spec.unit,
                description=spec.description,
            )
        _INSTRUMENTS[name] = (provider, created)
        return created
    except Exception:
        _logger.debug("metric_instrument_unavailable", metric=name, exc_info=True)
        return _NoopInstrument()


def record(name: str, amount: float = 1, attributes: Mapping[str, Any] | None = None) -> None:
    """Add to a counter or record a histogram value of catalogue entry ``name``; never raises."""
    try:
        target = instrument(name)
        if _SPECS[name].instrument == "histogram":
            target.record(amount, attributes)
        else:
            target.add(amount, attributes)
    except Exception:
        _logger.debug("metric_record_failed", metric=name, exc_info=True)


def _safe_add(target: Any, amount: float, attributes: Mapping[str, Any]) -> None:
    try:
        target.add(amount, attributes)
    except Exception:
        _logger.debug("metric_record_failed", exc_info=True)


def _safe_record(target: Any, amount: float, attributes: Mapping[str, Any]) -> None:
    try:
        target.record(amount, attributes)
    except Exception:
        _logger.debug("metric_record_failed", exc_info=True)


def _snapshot(read: Callable[[], Any]) -> Any:
    """``read()`` from a metric reader's thread while the event loop may be changing the dict it
    reads: retried, and empty if it keeps changing."""
    for _ in range(3):
        try:
            return read()
        except RuntimeError:
            continue
    return None


# Gauges observe every live object of a kind: an observable gauge's callbacks are fixed when it
# is first created, so one callback per gauge reads them all through these weak sets.
_RUN_METRICS: weakref.WeakSet[RunMetrics] = weakref.WeakSet()
_POOLS: weakref.WeakSet[Any] = weakref.WeakSet()


def _observation(value: float, attributes: Mapping[str, Any] | None = None) -> Any:
    return metrics.Observation(value, dict(attributes or {}))


def _observe_active_runs(options: Any) -> Iterable[Any]:
    total = 0
    for item in tuple(_RUN_METRICS):
        total += item.active_runs()
    return [_observation(total)]


def _observe_paused_runs(options: Any) -> Iterable[Any]:
    counts: Counter[str] = Counter({kind: 0 for kind in PAUSE_KINDS})
    for item in tuple(_RUN_METRICS):
        counts.update(item.paused_runs())
    return [_observation(count, {"pause_kind": kind}) for kind, count in sorted(counts.items())]


def _pool_sizes() -> tuple[int, int, int]:
    in_use = idle = maximum = 0
    for database in tuple(_POOLS):
        pool = getattr(database, "_pool", None)
        if pool is None:
            continue
        try:
            size, free = int(pool.get_size()), int(pool.get_idle_size())
            in_use += max(0, size - free)
            idle += free
            maximum += int(pool.get_max_size())
        except Exception:
            continue
    return in_use, idle, maximum


def _observe_pool_connections(options: Any) -> Iterable[Any]:
    in_use, idle, _ = _pool_sizes()
    return [_observation(in_use, {"state": "in_use"}), _observation(idle, {"state": "idle"})]


def _observe_pool_max(options: Any) -> Iterable[Any]:
    return [_observation(_pool_sizes()[2])]


def observe_postgres_pool(database: Any) -> None:
    """Report ``database``'s pool (an object with an asyncpg ``_pool``) in the pool gauges."""
    _POOLS.add(database)
    instrument("algen_agent_runtime.postgres.pool.connections", [_observe_pool_connections])
    instrument("algen_agent_runtime.postgres.pool.max_size", [_observe_pool_max])


def pause_kind(status: str, payload: Mapping[str, Any] | None) -> str:
    """The ``pause_kind`` of a run paused in ``status`` with ``pause_payload``."""
    kind = (payload or {}).get("kind")
    if status == "awaiting_approval":
        return "continuation" if kind == "execution_continuation" else "approval"
    if status == "awaiting_clarification":
        return "input" if kind == "ask_user" else "clarification"
    if status == "awaiting_reconciliation":
        return "reconciliation"
    if status == "awaiting_credentials":
        return "credentials"
    return "other"


def run_kind(request: Any) -> str:
    """How a run was started: ``delegated`` (a ``core.subagent`` child), ``workflow_step``,
    ``conversation_turn`` or ``run``."""
    if getattr(request, "parent_run_id", None):
        return "delegated"
    if getattr(request, "workflow_run_id", None):
        return "workflow_step"
    if getattr(request, "conversation_id", None) or getattr(request, "turn_id", None):
        return "conversation_turn"
    return "run"


class RunMetrics:
    """The run, approval and guard metrics of one ``AgentRuntime``.

    ``tasks`` is the runtime's own map of the drives executing in this process (read by the
    ``runs.active`` gauge). Paused runs are tracked by run id from their pause until they
    continue or end, and seeded by ``recover()`` with the runs already paused at a restart.
    """

    def __init__(self, tasks: Mapping[str, Any]) -> None:
        self._tasks = tasks
        self._paused: dict[str, str] = {}
        self.runs_started = instrument("algen_agent_runtime.runs.started")
        self.runs_finished = instrument("algen_agent_runtime.runs.finished")
        self.run_duration = instrument("algen_agent_runtime.run.duration")
        self.pauses = instrument("algen_agent_runtime.run.pauses")
        self.resumes = instrument("algen_agent_runtime.run.resumes")
        self.recoveries = instrument("algen_agent_runtime.run.recoveries")
        self.approvals_requested = instrument("algen_agent_runtime.approvals.requested")
        self.approvals_resolved = instrument("algen_agent_runtime.approvals.resolved")
        self.approval_wait = instrument("algen_agent_runtime.approval.wait.duration")
        self.tool_refusals = instrument("algen_agent_runtime.tool.refusals")
        instrument("algen_agent_runtime.runs.active", [_observe_active_runs])
        instrument("algen_agent_runtime.runs.paused", [_observe_paused_runs])
        _RUN_METRICS.add(self)

    # Gauges (called from the metric reader's thread)
    def active_runs(self) -> int:
        tasks = _snapshot(lambda: tuple(self._tasks.values()))
        return sum(1 for task in tasks or () if not task.done())

    def paused_runs(self) -> Counter[str]:
        kinds = _snapshot(lambda: tuple(self._paused.values()))
        return Counter(kinds or ())

    # Runs
    def started(self, state: Any) -> None:
        _safe_add(self.runs_started, 1, self._run_attributes(state))

    def finished(self, state: Any, reason: str | None) -> None:
        self._paused.pop(state.id, None)
        status = getattr(state.status, "value", str(state.status))
        attributes = self._run_attributes(state)
        _safe_add(
            self.runs_finished,
            1,
            {
                **attributes,
                "status": status,
                # A cancelled run is no error (0.2.0b5): it ends "cancelled" unless its end
                # event names a reason (a run timeout does).
                "reason": reason or _DEFAULT_END_REASON.get(status, "error"),
            },
        )
        started_at = getattr(state, "started_at", None)
        if started_at is not None:
            elapsed = max(0.0, (_utc_now() - started_at).total_seconds())
            _safe_record(self.run_duration, elapsed, {**attributes, "status": status})

    def paused(self, state: Any) -> str:
        kind = pause_kind(getattr(state.status, "value", str(state.status)), state.pause_payload)
        self._paused[state.id] = kind
        _safe_add(self.pauses, 1, {"pause_kind": kind})
        if kind in {"approval", "continuation"}:
            _safe_add(self.approvals_requested, 1, {"approval_kind": _approval_kind(kind)})
        return kind

    def seen_paused(self, state: Any) -> None:
        """A run found paused (at recovery): counted in ``runs.paused`` without a new pause."""
        status = getattr(state.status, "value", str(state.status))
        self._paused[state.id] = pause_kind(status, state.pause_payload)

    def left_pause(self, run_id: str, previous_status: str, *, ended: bool) -> None:
        """A paused run continued, or (``ended``) was cancelled or timed out."""
        kind = self._paused.pop(run_id, None) or pause_kind(previous_status, None)
        if not ended:
            _safe_add(self.resumes, 1, {"pause_kind": kind})

    def recovered(self, trigger: str, outcome: str) -> None:
        _safe_add(self.recoveries, 1, {"trigger": trigger, "outcome": outcome})

    # Approvals
    def approval_resolved(self, payload_kind: Any, outcome: str, created_at: Any) -> None:
        kind = "continuation" if payload_kind == "execution_continuation" else "tool"
        _safe_add(self.approvals_resolved, 1, {"approval_kind": kind, "outcome": outcome})
        if created_at is not None:
            try:
                elapsed = max(0.0, (_utc_now() - created_at).total_seconds())
            except TypeError:
                return
            _safe_record(self.approval_wait, elapsed, {"approval_kind": kind, "outcome": outcome})

    def tool_refused(self, tool: str, status: str) -> None:
        _safe_add(self.tool_refusals, 1, {"tool": tool, "status": status})

    @staticmethod
    def _run_attributes(state: Any) -> dict[str, str]:
        request = state.request
        return {"agent": str(request.agent), "run_kind": run_kind(request)}


def _approval_kind(pause: str) -> str:
    return "continuation" if pause == "continuation" else "tool"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Stopwatch:
    """Seconds since it was made, by the monotonic clock."""

    __slots__ = ("_started",)

    def __init__(self) -> None:
        self._started = time.monotonic()

    def seconds(self) -> float:
        return time.monotonic() - self._started
