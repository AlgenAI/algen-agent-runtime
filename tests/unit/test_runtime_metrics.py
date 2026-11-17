"""The run-level metrics: what a few runs record, with low-cardinality attributes only, and
that every metric the shipped dashboards and alert rules query is one the code records.

The OpenTelemetry meter provider is process-global and can be set once, so the fixture installs
an SDK provider with an in-memory reader for one test and puts the API's globals back after it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from conftest import make_agent, make_runtime
from opentelemetry import metrics
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    Gauge,
    Histogram,
    InMemoryMetricReader,
    Sum,
)
from opentelemetry.util._once import Once

from algen_agent_runtime.api.app import create_app
from algen_agent_runtime.auth.audit import AuthenticationAuditor
from algen_agent_runtime.auth.contracts import AuthenticationFailure
from algen_agent_runtime.config.settings import ApiRateLimitSettings, ApiSettings, AppSettings
from algen_agent_runtime.conversations import (
    ConversationHandlerRegistry,
    ConversationService,
    MessageStatus,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.events.bus import InMemoryEventBus
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.exceptions.errors import (
    EventStreamOverflowError,
    InvalidToolCallError,
    ProviderError,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.observability import metrics as metrics_module
from algen_agent_runtime.observability.metrics import (
    PAUSE_KINDS,
    RUNTIME_METRICS,
    MetricSpec,
    metric_spec,
    observe_postgres_pool,
    prometheus_name,
)
from algen_agent_runtime.observability.metrics import record as record_metric
from algen_agent_runtime.persistence.postgres import _unavailable as postgres_unavailable
from algen_agent_runtime.persistence.redis import redis_unavailable
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    GuardrailPolicy,
    RunRequest,
    RunState,
    RunStatus,
    ToolPolicy,
    utc_now,
)
from algen_agent_runtime.workflows import MultiAgentWorkflowExecutor, WorkflowHookRegistry
from algen_agent_runtime.workflows.contracts import (
    WorkflowApprovalRule,
    WorkflowManifest,
    WorkflowNode,
    WorkflowNodeKind,
    WorkflowStatus,
)

ROOT = Path(__file__).resolve().parents[2]
OBSERVABILITY = ROOT / "deploy" / "observability"
TENANT = "tenant-metrics"
USER = "user-metrics"
# Values the scenario uses that must never become a metric attribute.
IDENTIFIERS = (TENANT, USER)


def _reset_meter_provider() -> None:
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()
    metrics_internal._METER_PROVIDER = None
    metrics_internal._PROXY_METER_PROVIDER = metrics_internal._ProxyMeterProvider()


@pytest.fixture
def reader() -> Iterator[InMemoryMetricReader]:
    _reset_meter_provider()
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    # The run gauges sum every live runtime in the process: leave out other tests' runtimes.
    earlier = list(metrics_module._RUN_METRICS)
    metrics_module._RUN_METRICS.clear()
    try:
        yield reader
    finally:
        provider.shutdown()
        _reset_meter_provider()
        metrics_module._RUN_METRICS.update(earlier)


@dataclass(frozen=True)
class Point:
    attributes: dict[str, Any]
    value: float


@dataclass
class Recorded:
    kind: str
    unit: str
    points: list[Point]


def collect(reader: InMemoryMetricReader) -> dict[str, Recorded]:
    data = reader.get_metrics_data()
    found: dict[str, Recorded] = {}
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if isinstance(metric.data, Histogram):
                    kind = "histogram"
                    points = [
                        Point(dict(point.attributes or {}), point.count)
                        for point in metric.data.data_points
                    ]
                elif isinstance(metric.data, Sum):
                    kind = "counter" if metric.data.is_monotonic else "up_down_counter"
                    points = [
                        Point(dict(point.attributes or {}), point.value)
                        for point in metric.data.data_points
                    ]
                elif isinstance(metric.data, Gauge):
                    kind = "gauge"
                    points = [
                        Point(dict(point.attributes or {}), point.value)
                        for point in metric.data.data_points
                    ]
                else:  # pragma: no cover - no other instrument is used
                    continue
                found[metric.name] = Recorded(kind, metric.unit or "", points)
    return found


def total(recorded: Mapping[str, Recorded], name: str, **attributes: Any) -> float:
    item = recorded.get(name)
    if item is None:
        return 0
    return sum(
        point.value
        for point in item.points
        if all(point.attributes.get(key) == value for key, value in attributes.items())
    )


# --- the scenario -----------------------------------------------------------------------------


def _tool(name: str, *, side_effect: SideEffect = SideEffect.READ, **options: Any) -> Tool:
    return Tool(
        ToolDefinition(
            name=name,
            version="1",
            description=name,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            side_effect=side_effect,
            **options,
        ),
        lambda args, ctx: {"ok": True},
    )


class DenyDeletes:
    """Denies every call to ``files.delete`` before it runs."""

    name = "deny-deletes"
    category = "test"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.BEFORE_TOOL})

    async def evaluate(self, point: str, payload: Any, context: Mapping[str, Any]) -> Any:
        tool = context.get("tool")
        if getattr(tool, "name", None) == "files.delete":
            return PolicyDecision(
                action=PolicyAction.DENY, reason_code="test.deny", reason="no deletes"
            )
        return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ok", reason="ok")


class Broken:
    """Raises at the tool boundary; its fail mode is open, so the call goes on."""

    name = "broken"
    category = "test"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.BEFORE_TOOL})

    async def evaluate(self, point: str, payload: Any, context: Mapping[str, Any]) -> Any:
        raise RuntimeError("policy bug")


class FailingProvider(MockModelProvider):
    async def generate(self, request: Any) -> Any:
        raise ProviderError("down", ErrorKind.UNAVAILABLE, retryable=False)


def _request(**updates: Any) -> RunRequest:
    return RunRequest(
        agent="test-agent", input="metrics", tenant_id=TENANT, user_id=USER, **updates
    )


async def _until(runtime: AgentRuntime, run_id: str, *statuses: RunStatus) -> RunState:
    for _ in range(600):
        state = await runtime.status(run_id, TENANT)
        if state.status in statuses:
            return state
        await asyncio.sleep(0.005)
    raise AssertionError(f"run never reached {statuses}: {state.status}")


async def _completed_run_with_tools() -> None:
    """A read tool, the same read again (the repeat guard holds it back), a denied delete and
    a tool whose broken policy fails open; then the answer."""
    provider = MockModelProvider(
        [
            tool_call_response("docs.read", {"page": 1}),
            tool_call_response("docs.read", {"page": 1}),
            tool_call_response("files.delete", {"path": "x"}),
            "done",
        ]
    )
    agent = make_agent(
        enabled_tools=frozenset({"docs.read", "files.delete"}),
        tool_policy=ToolPolicy(repeat_guard=True, on_error="return_to_model"),
        guardrail_policy=GuardrailPolicy(policies=("deny-deletes",)),
    )
    runtime = make_runtime(provider, agent)
    runtime.policies.register(DenyDeletes())
    runtime.tools.register(_tool("docs.read"))
    runtime.tools.register(_tool("files.delete", side_effect=SideEffect.NONE))
    try:
        result = await runtime.run(_request())
        assert result.status == RunStatus.COMPLETED, result
    finally:
        await runtime.shutdown(grace_seconds=5)


async def _policy_error() -> None:
    from algen_agent_runtime.policies.contracts import PolicyErrorHandling, PolicyFailMode

    provider = MockModelProvider([tool_call_response("docs.read", {}), "done"])
    agent = make_agent(
        enabled_tools=frozenset({"docs.read"}),
        guardrail_policy=GuardrailPolicy(policies=("broken",)),
    )
    runtime = make_runtime(provider, agent)
    runtime.policies.error_handling = PolicyErrorHandling(on_error=PolicyFailMode.OPEN)
    runtime.policies.register(Broken())
    runtime.tools.register(_tool("docs.read"))
    try:
        await runtime.run(_request())
    finally:
        await runtime.shutdown(grace_seconds=5)


async def _approval_round_trips() -> AgentRuntime:
    """One approval approved, one left to expire, and one run left paused (returned)."""
    provider = MockModelProvider(
        [
            tool_call_response("mail.send", {"to": "a"}),
            "sent",
            tool_call_response("mail.send", {"to": "b"}),
            tool_call_response("mail.send", {"to": "c"}),
        ]
    )
    runtime = make_runtime(provider, make_agent(enabled_tools=frozenset({"mail.send"})))
    runtime.tools.register(_tool("mail.send", side_effect=SideEffect.EXTERNAL))

    approved = await runtime.start(_request())
    await _until(runtime, approved.id, RunStatus.AWAITING_APPROVAL)
    await runtime.resume(approved.id, TENANT, {"decision": "approved"}, decided_by=USER)
    assert (await runtime.wait(approved.id, TENANT)).status == RunStatus.COMPLETED

    expiring = await runtime.start(_request())
    expiring = await _until(runtime, expiring.id, RunStatus.AWAITING_APPROVAL)
    approval_id = str((expiring.pause_payload or {})["approval_id"])
    items = runtime.approvals._items  # type: ignore[attr-defined]
    items[approval_id] = items[approval_id].model_copy(
        update={"expires_at": utc_now() - timedelta(seconds=1)}
    )
    assert await runtime.expire_paused_runs() == 1

    waiting = await runtime.start(_request())
    await _until(runtime, waiting.id, RunStatus.AWAITING_APPROVAL)
    return runtime


async def _failed_run() -> None:
    runtime = make_runtime(FailingProvider(["never"]))
    try:
        result = await runtime.run(_request())
        assert result.status == RunStatus.FAILED
    finally:
        await runtime.shutdown(grace_seconds=5)


class OneBadToolCall(MockModelProvider):
    """Writes one tool call that is not a JSON object, then answers."""

    def __init__(self) -> None:
        super().__init__(["fixed"])
        self.failed = False

    async def generate(self, request: Any) -> Any:
        if not self.failed:
            self.failed = True
            raise InvalidToolCallError("mock", "docs.read", "not a JSON object")
        return await super().generate(request)


async def _invalid_tool_call() -> None:
    runtime = make_runtime(OneBadToolCall())
    try:
        assert (await runtime.run(_request())).status == RunStatus.COMPLETED
    finally:
        await runtime.shutdown(grace_seconds=5)


async def _authentication_failure() -> None:
    auditor = AuthenticationAuditor(None)
    await auditor.failure(AuthenticationFailure("malformed_token", method="jwt"))


async def _recovered_run() -> None:
    runtime = make_runtime(MockModelProvider(["recovered"]))
    agent = runtime.agents.get("test-agent")
    state = RunState(
        request=_request(),
        agent_key=agent.key,
        status=RunStatus.PLANNING,
        deadline=utc_now() + timedelta(minutes=5),
    )
    await runtime.runs.create(state)
    try:
        assert await runtime.recover() == 1
        assert (await runtime.wait(state.id, TENANT)).status == RunStatus.COMPLETED
    finally:
        await runtime.shutdown(grace_seconds=5)


async def _conversation_turn() -> None:
    runtime = make_runtime(MockModelProvider(["hello"]))
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    service = ConversationService(
        InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
    )
    conversation = await service.create(tenant_id=TENANT, user_id=USER, agent="test-agent")
    await service.submit(conversation.id, TENANT, USER, "hi")
    for _ in range(600):
        messages = await service.messages(conversation.id, TENANT)
        if messages and messages[-1].status == MessageStatus.COMPLETED:
            break
        await asyncio.sleep(0.005)
    else:
        raise AssertionError("the turn never completed")
    await service.shutdown(grace_seconds=5)
    await runtime.shutdown(grace_seconds=5)


async def _workflow_run() -> None:
    runtime = make_runtime()
    runtime.tools.register(_tool("evidence.collect"))
    manifest = WorkflowManifest(
        name="release",
        version="1.0.0",
        output_key="evidence",
        nodes=(
            WorkflowNode(
                id="evidence",
                kind=WorkflowNodeKind.HANDLER,
                handler="tool:evidence.collect",
                arguments={},
                output_key="evidence",
            ),
        ),
    )
    state = await MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry()).run(
        manifest, {}, tenant_id=TENANT, user_id=USER
    )
    assert state.status == WorkflowStatus.COMPLETED
    # An approval node, approved.
    gated = WorkflowManifest(
        name="gated",
        version="1.0.0",
        output_key="review",
        nodes=(
            WorkflowNode(
                id="review",
                kind=WorkflowNodeKind.APPROVAL,
                output_key="review",
                approval=WorkflowApprovalRule(prompt="Ship?"),
            ),
        ),
    )
    executor = MultiAgentWorkflowExecutor(runtime, WorkflowHookRegistry())
    paused = await executor.run(gated, {}, tenant_id=TENANT, user_id=USER)
    assert paused.status == WorkflowStatus.AWAITING_APPROVAL
    done = await executor.decide_approval(
        gated, paused.id, tenant_id=TENANT, user_id=USER, decision="approved"
    )
    assert done.status == WorkflowStatus.COMPLETED


async def _api_requests() -> None:
    settings = AppSettings(
        api=ApiSettings(
            rate_limiting=ApiRateLimitSettings(
                enabled=True, default_rate_per_minute=60, default_burst=1
            )
        )
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    headers = {"x-tenant-id": TENANT, "x-user-id": USER, "x-scopes": "runs:read"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/v1/runs/run-404", headers=headers)).status_code == 404
        assert (await client.get("/v1/runs/run-429", headers=headers)).status_code == 429
        assert (await client.get("/health/live")).status_code == 200


async def _event_backpressure() -> None:
    bus = InMemoryEventBus(history_limit=2, subscriber_queue_size=1)

    def event(sequence: int) -> RunEvent:
        return RunEvent(
            type="tool.note",
            run_id="run-bp",
            tenant_id=TENANT,
            session_id="s",
            correlation_id="c",
            sequence=sequence,
        )

    await bus.publish(event(1))
    stream = bus.subscribe("run-bp").__aiter__()
    assert (await stream.__anext__()).sequence == 1
    reading = asyncio.ensure_future(stream.__anext__())
    await asyncio.sleep(0)
    for sequence in range(2, 8):
        await bus.publish(event(sequence))
    assert (await reading).sequence == 2
    with pytest.raises(EventStreamOverflowError):
        while True:
            await stream.__anext__()


class _Pool:
    def get_size(self) -> int:
        return 5

    def get_idle_size(self) -> int:
        return 2

    def get_max_size(self) -> int:
        return 10


class _Database:
    _pool = _Pool()


async def exercise(reader: InMemoryMetricReader) -> dict[str, Recorded]:
    await _completed_run_with_tools()
    await _policy_error()
    paused = await _approval_round_trips()
    await _failed_run()
    await _invalid_tool_call()
    await _authentication_failure()
    await _recovered_run()
    await _conversation_turn()
    await _workflow_run()
    await _api_requests()
    await _event_backpressure()
    postgres_unavailable(OSError("connection refused"))

    class ConnectionError(Exception):
        __module__ = "redis.exceptions"

    redis_unavailable(ConnectionError())
    database = _Database()
    observe_postgres_pool(database)
    try:
        return collect(reader)
    finally:
        await paused.shutdown(grace_seconds=5)


# --- the tests --------------------------------------------------------------------------------


async def test_runs_record_the_run_level_metrics(reader: InMemoryMetricReader) -> None:
    recorded = await exercise(reader)

    started = total(recorded, "algen_agent_runtime.runs.started", agent="test-agent")
    finished = total(recorded, "algen_agent_runtime.runs.finished", agent="test-agent")
    assert started == 8
    # Every run started here has ended except the one still paused for its approval; the run
    # recovered after a "restart" ended without starting in this process.
    assert finished == started
    assert total(recorded, "algen_agent_runtime.runs.finished", status="completed") == 6
    assert (
        total(
            recorded, "algen_agent_runtime.runs.finished", status="failed", reason="ProviderError"
        )
        >= 1
    )
    assert (
        total(
            recorded,
            "algen_agent_runtime.runs.finished",
            status="timed_out",
            reason="approval_expired",
        )
        == 1
    )
    assert total(recorded, "algen_agent_runtime.runs.started", run_kind="conversation_turn") == 1
    assert total(recorded, "algen_agent_runtime.run.duration", status="completed") == 6

    # Approvals: three requested, one approved, one expired, one still waiting.
    assert total(recorded, "algen_agent_runtime.approvals.requested", approval_kind="tool") == 3
    tool = {"approval_kind": "tool"}
    assert (
        total(recorded, "algen_agent_runtime.approvals.resolved", outcome="approved", **tool) == 1
    )
    assert total(recorded, "algen_agent_runtime.approvals.resolved", outcome="expired", **tool) == 1
    assert total(recorded, "algen_agent_runtime.approval.wait.duration", **tool) == 2
    # The workflow approval node (in the workflow scenario) is counted as approval_kind workflow.
    assert (
        total(recorded, "algen_agent_runtime.approval.wait.duration", approval_kind="workflow") == 1
    )
    assert total(recorded, "algen_agent_runtime.run.pauses", pause_kind="approval") == 3
    assert total(recorded, "algen_agent_runtime.run.resumes", pause_kind="approval") == 1
    paused = {
        point.attributes["pause_kind"]: point.value
        for point in recorded["algen_agent_runtime.runs.paused"].points
    }
    assert paused == {kind: (1 if kind == "approval" else 0) for kind in PAUSE_KINDS}
    assert total(recorded, "algen_agent_runtime.runs.active") == 0
    assert (
        total(recorded, "algen_agent_runtime.run.recoveries", trigger="restart", outcome="driven")
        == 1
    )

    # Models and tools.
    assert total(recorded, "algen_agent_runtime.model.calls", provider="mock", outcome="ok") >= 10
    assert (
        total(
            recorded,
            "algen_agent_runtime.model.calls",
            outcome="error",
            error_kind="unavailable",
        )
        == 1
    )
    assert total(recorded, "algen_agent_runtime.model.tokens", token_type="input") > 0
    assert total(recorded, "algen_agent_runtime.model.tokens", token_type="output") > 0
    assert total(recorded, "algen_agent_runtime.model.call.duration", purpose="step") >= 10
    assert (
        total(recorded, "algen_agent_runtime.tool.calls", tool="docs.read", outcome="completed")
        >= 2
    )
    assert (
        total(recorded, "algen_agent_runtime.tool.calls", tool="files.delete", outcome="denied")
        == 1
    )
    assert (
        total(recorded, "algen_agent_runtime.tool.refusals", tool="docs.read", status="repeated")
        == 1
    )
    assert total(recorded, "algen_agent_runtime.tool.call.duration", tool="mail.send") == 1
    assert (
        total(
            recorded,
            "algen_agent_runtime.policy.denials",
            boundary="before_tool",
            policy="deny-deletes",
        )
        == 1
    )
    assert total(recorded, "algen_agent_runtime.policy.errors", policy="broken") >= 1
    assert total(recorded, "algen_agent_runtime.model.tool_calls_invalid", cut_off=False) == 1
    assert total(recorded, "algen_agent_runtime.auth.attempts", outcome="failure") == 1

    # API, stores, events, conversations and workflows.
    assert total(
        recorded, "algen_agent_runtime.api.requests", route="/v1/runs/{run_id}", status_code="404"
    ) == (1)
    assert total(
        recorded, "algen_agent_runtime.api.requests", route="/v1/runs/{run_id}", status_code="429"
    ) == (1)
    assert total(
        recorded,
        "algen_agent_runtime.api.admission.rejections",
        route_class="read",
        reason="rate_limit_exceeded",
    ) == (1)
    assert total(recorded, "algen_agent_runtime.store.unavailable", store="postgres") == 1
    assert total(recorded, "algen_agent_runtime.store.unavailable", store="redis") == 1
    assert total(recorded, "algen_agent_runtime.postgres.pool.connections", state="in_use") == 3
    assert total(recorded, "algen_agent_runtime.postgres.pool.max_size") == 10
    assert total(recorded, "algen_agent_runtime.events.backpressure", kind="subscriber_lagged") >= 1
    assert total(recorded, "algen_agent_runtime.events.backpressure", kind="stream_overflow") == 1
    assert total(recorded, "algen_agent_runtime.conversation.turns", outcome="completed") == 1
    assert (
        total(
            recorded,
            "algen_agent_runtime.workflow.runs.finished",
            workflow="release",
            status="completed",
        )
        == 1
    )
    assert total(recorded, "algen_agent_runtime.approvals.requested", approval_kind="workflow") == 1
    assert (
        total(
            recorded,
            "algen_agent_runtime.approvals.resolved",
            approval_kind="workflow",
            outcome="approved",
        )
        == 1
    )

    # Every instrument matches its catalogue entry, and no attribute is an id or content.
    for name, item in recorded.items():
        if not name.startswith("algen_agent_runtime."):
            continue
        spec = metric_spec(name)
        assert item.kind == spec.instrument, name
        assert item.unit == spec.unit, name
        for point in item.points:
            assert set(point.attributes) <= set(spec.attributes), (name, point.attributes)
            for value in point.attributes.values():
                assert all(identifier not in str(value) for identifier in IDENTIFIERS), name
    del recorded


# --- dashboards, alerts and docs --------------------------------------------------------------

_SERIES = re.compile(r"\balgen_agent_runtime_[a-z0-9_]+")
_HISTOGRAM_SERIES = ("_bucket", "_sum", "_count")


def _prometheus_index() -> dict[str, MetricSpec]:
    index: dict[str, MetricSpec] = {}
    for spec in RUNTIME_METRICS:
        name = prometheus_name(spec)
        if spec.instrument == "histogram":
            for suffix in _HISTOGRAM_SERIES:
                index[name + suffix] = spec
        else:
            index[name] = spec
    return index


def _alert_rules() -> list[dict[str, Any]]:
    document = yaml.safe_load((OBSERVABILITY / "prometheus" / "alerts.yml").read_text())
    return [rule for group in document["groups"] for rule in group["rules"]]


def _dashboards() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text())
        for path in sorted((OBSERVABILITY / "grafana").glob("*.json"))
    }


def _dashboard_queries(dashboard: Mapping[str, Any]) -> list[str]:
    queries: list[str] = []
    for panel in dashboard["panels"]:
        for target in panel.get("targets", ()):
            queries.append(target["expr"])
    return queries


def _referenced_specs() -> dict[str, MetricSpec]:
    index = _prometheus_index()
    texts = [rule["expr"] for rule in _alert_rules()]
    for dashboard in _dashboards().values():
        texts += _dashboard_queries(dashboard)
    found: dict[str, MetricSpec] = {}
    for text in texts:
        for series in _SERIES.findall(text):
            assert series in index, f"{series} is not a metric the Runtime records"
            found[series] = index[series]
    return found


def test_alert_rules_are_valid_prometheus_rules() -> None:
    rules = _alert_rules()
    assert rules
    names = [rule["alert"] for rule in rules]
    assert len(names) == len(set(names))
    for rule in rules:
        assert set(rule) <= {"alert", "expr", "for", "labels", "annotations", "keep_firing_for"}
        assert rule["labels"]["severity"] in {"page", "ticket", "security"}
        assert rule["annotations"]["summary"] and rule["annotations"]["runbook_url"]
        assert re.fullmatch(r"\d+[smh]", rule.get("for", "0s")), rule["alert"]
        # Balanced brackets: a cheap guard against a broken PromQL edit.
        for opening, closing in ("()", "[]", "{}"):
            assert rule["expr"].count(opening) == rule["expr"].count(closing), rule["alert"]


def test_dashboards_are_valid_grafana_json() -> None:
    dashboards = _dashboards()
    assert dashboards
    for name, dashboard in dashboards.items():
        assert dashboard["uid"] and dashboard["title"] and dashboard["schemaVersion"] >= 36, name
        ids = [panel["id"] for panel in dashboard["panels"]]
        assert len(ids) == len(set(ids)), name
        assert _dashboard_queries(dashboard), name
        variables = {item["name"] for item in dashboard["templating"]["list"]}
        assert "datasource" in variables, name
        for panel in dashboard["panels"]:
            for target in panel.get("targets", ()):
                assert target["datasource"]["uid"] == "${datasource}", (name, panel["title"])


def test_every_metric_the_dashboards_and_alerts_query_is_documented() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text()
    for spec in RUNTIME_METRICS:
        assert f"`{spec.name}`" in operations, spec.name
        assert f"`{prometheus_name(spec)}`" in operations, spec.name
    assert _referenced_specs()


async def test_every_metric_the_dashboards_and_alerts_query_is_recorded(
    reader: InMemoryMetricReader,
) -> None:
    recorded = await exercise(reader)
    missing = sorted(
        {spec.name for spec in _referenced_specs().values() if spec.name not in recorded}
    )
    assert not missing, f"dashboards or alerts query metrics no run recorded: {missing}"


def test_prometheus_names_follow_the_otlp_translation() -> None:
    assert prometheus_name("algen_agent_runtime.run.duration") == (
        "algen_agent_runtime_run_duration_seconds"
    )
    assert prometheus_name("algen_agent_runtime.runs.started") == (
        "algen_agent_runtime_runs_started_total"
    )
    assert prometheus_name("algen_agent_runtime.runs.active") == "algen_agent_runtime_runs_active"
    assert prometheus_name("algen_agent_runtime.cache.operation.duration") == (
        "algen_agent_runtime_cache_operation_duration_milliseconds"
    )
    assert len({spec.name for spec in RUNTIME_METRICS}) == len(RUNTIME_METRICS)


def _anchors(markdown: str) -> set[str]:
    anchors = set()
    for line in markdown.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            anchors.add(re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading)))
    return anchors


def test_alert_runbooks_and_dashboard_links_point_at_existing_sections() -> None:
    anchors = _anchors((ROOT / "docs" / "operations.md").read_text())
    links = [rule["annotations"]["runbook_url"] for rule in _alert_rules()]
    for dashboard in _dashboards().values():
        links += [link["url"] for link in dashboard["links"]]
    for link in links:
        page, _, anchor = link.partition("#")
        assert page.endswith("/docs/operations.md"), link
        assert anchor in anchors, link


def test_recording_without_a_meter_provider_creates_each_instrument_once() -> None:
    _reset_meter_provider()
    try:
        proxy = metrics_internal._PROXY_METER_PROVIDER
        for _ in range(100):
            record_metric(
                "algen_agent_runtime.policy.denials", 1, {"boundary": "input", "policy": "p"}
            )
        # The API keeps every proxy meter it hands out until a provider is installed.
        assert len(proxy._meters) == 1
    finally:
        _reset_meter_provider()


async def test_a_cancelled_run_finishes_with_reason_cancelled(
    reader: InMemoryMetricReader,
) -> None:
    from algen_agent_runtime.tools.ask_user import ASK_USER_PERMISSION, ASK_USER_TOOL

    runtime = make_runtime(
        MockModelProvider([tool_call_response(ASK_USER_TOOL, {"question": "Which?"})]),
        make_agent(
            enabled_tools=frozenset({ASK_USER_TOOL}),
            tool_permissions=frozenset({ASK_USER_PERMISSION}),
        ),
    )
    paused = await runtime.run(_request())
    await runtime.cancel(paused.run_id, TENANT)
    await runtime.wait(paused.run_id, TENANT)
    recorded = collect(reader)
    assert (
        total(recorded, "algen_agent_runtime.runs.finished", status="cancelled", reason="cancelled")
        == 1
    )
    assert total(recorded, "algen_agent_runtime.runs.finished", reason="error") == 0


def test_the_runtimes_providers_report_the_configured_service_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algen_agent_runtime.observability.setup import service_resource

    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    assert service_resource("billing-agents").attributes["service.name"] == "billing-agents"
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-environment")
    assert service_resource("billing-agents").attributes["service.name"] == "from-environment"
