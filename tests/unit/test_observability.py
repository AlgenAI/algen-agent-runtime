from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import make_runtime  # type: ignore[import-not-found]

from algen_agent_runtime.config.settings import AppSettings, TracciaSettings
from algen_agent_runtime.conversations.service import (
    ConversationHandlerRegistry,
    ConversationService,
    RuntimeConversationHandler,
)
from algen_agent_runtime.conversations.stores import (
    InMemoryConversationEventBus,
    InMemoryConversationStore,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.observability.setup import configure_telemetry
from algen_agent_runtime.observability.traccia_adapter import (
    NoopObservabilityAdapter,
    TracciaObservabilityAdapter,
    observability_adapter,
)
from algen_agent_runtime.observability.trace_levels import TraceLevelTracer
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import RunRequest


class FakeRuntimeConfig:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self._calls = calls

    @contextmanager
    def run_identity(self, **values: Any) -> Iterator[None]:
        self._calls.append(("run_identity", values))
        yield


def test_traccia_configuration_is_strict_and_disabled_by_default() -> None:
    settings = AppSettings()
    assert settings.telemetry.traccia.enabled is False
    assert settings.telemetry.trace_level == "detailed"
    assert isinstance(observability_adapter(settings.telemetry), NoopObservabilityAdapter)
    with pytest.raises(ValueError, match="env://"):
        TracciaSettings(enabled=True, api_key="literal-secret")


def test_global_telemetry_switch_disables_traccia_adapter() -> None:
    settings = AppSettings(
        telemetry={
            "enabled": False,
            "traccia": {"enabled": True, "api_key": "env://TRACCIA_API_KEY"},
        }
    )
    assert isinstance(observability_adapter(settings.telemetry), NoopObservabilityAdapter)


def test_traccia_adapter_lifecycle_and_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    fake = SimpleNamespace(
        init=lambda **options: calls.append(("init", options)),
        runtime_config=FakeRuntimeConfig(calls),
        force_flush=lambda timeout: calls.append(("flush", timeout)),
    )
    monkeypatch.setenv("TRACCIA_TEST_KEY", "secret-value")
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: fake,
    )
    adapter = TracciaObservabilityAdapter(
        TracciaSettings(
            enabled=True,
            api_key="env://TRACCIA_TEST_KEY",
            endpoint="https://api.traccia.ai/v2/traces",
            metrics_endpoint="https://api.traccia.ai/v2/metrics",
            environment="test",
            project_id="talk-to-data",
            sample_rate=0.5,
        ),
        "algen-agent-runtime-test",
    )
    registered: list[Any] = []
    monkeypatch.setattr(adapter, "_register_otel_provider", registered.append)

    adapter.start()
    adapter.start()
    with adapter.run_scope(agent_id="agent@1.0.0", agent_name="agent", tenant_id="tenant"):
        pass
    adapter.stop()

    init_calls = [value for name, value in calls if name == "init"]
    assert len(init_calls) == 1
    assert registered == [None]
    assert init_calls[0]["api_key"] == "secret-value"
    assert init_calls[0]["service_role"] == "orchestrator"
    assert init_calls[0]["env"] == "test"
    assert init_calls[0]["metrics_endpoint"] == "https://api.traccia.ai/v2/metrics"
    assert init_calls[0]["auto_start_trace"] is False
    assert init_calls[0]["enable_patching"] is False
    assert (
        "run_identity",
        {
            "agent_id": "agent@1.0.0",
            "agent_name": "agent",
            "env": "test",
            "tenant_id": "tenant",
            "project_id": "talk-to-data",
        },
    ) in calls
    assert ("flush", 5.0) in calls
    assert adapter.span_attributes() == {
        "env": "test",
        "environment": "test",
    }


def test_traccia_adapter_resolves_container_scoped_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    fake = SimpleNamespace(
        init=lambda **options: calls.append(options),
        force_flush=lambda timeout: None,
    )
    monkeypatch.delenv("TRACCIA_SCOPED_KEY", raising=False)
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: fake,
    )
    adapter = TracciaObservabilityAdapter(
        TracciaSettings(enabled=True, api_key="env://TRACCIA_SCOPED_KEY"),
        "algen-agent-runtime-test",
        environment={"TRACCIA_SCOPED_KEY": "studio-injected-value"},
    )
    monkeypatch.setattr(adapter, "_register_otel_provider", lambda provider: None)

    adapter.start()

    assert calls[0]["api_key"] == "studio-injected-value"


@pytest.mark.asyncio
async def test_container_wires_scoped_environment_into_traccia(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    fake = SimpleNamespace(
        init=lambda **options: calls.append(options),
        force_flush=lambda timeout: None,
    )
    monkeypatch.delenv("TRACCIA_CONTAINER_KEY", raising=False)
    real_import_module = importlib.import_module
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: fake if name == "traccia" else real_import_module(name),
    )
    monkeypatch.setattr(
        TracciaObservabilityAdapter,
        "_register_otel_provider",
        lambda self, provider: None,
    )
    settings = AppSettings.model_validate(
        {
            "telemetry": {
                "traccia": {
                    "enabled": True,
                    "api_key": "env://TRACCIA_CONTAINER_KEY",
                }
            }
        }
    )
    container = build_container(
        settings,
        environment={"TRACCIA_CONTAINER_KEY": "studio-injected-value"},
    )

    await container.astart()
    await container.aclose()

    assert calls[0]["api_key"] == "studio-injected-value"


def test_default_telemetry_does_not_claim_global_otel_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_sets: list[Any] = []
    meter_sets: list[Any] = []
    monkeypatch.setattr(
        "algen_agent_runtime.observability.setup.trace.set_tracer_provider",
        provider_sets.append,
    )
    monkeypatch.setattr(
        "algen_agent_runtime.observability.setup.metrics.set_meter_provider",
        meter_sets.append,
    )

    configure_telemetry(AppSettings().telemetry)

    assert provider_sets == []
    assert meter_sets == []


def test_traccia_adapter_uses_sdk_guardrail_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    fake = SimpleNamespace(
        init=lambda **options: calls.append(("init", options)),
        runtime_config=FakeRuntimeConfig(calls),
        stop_tracing=lambda timeout: None,
    )

    @contextmanager
    def guardrail_span(name: str, **attributes: Any) -> Iterator[RecordedSpan]:
        calls.append((name, attributes))
        yield RecordedSpan(name=name, parent=None)

    guardrails = SimpleNamespace(guardrail_span=guardrail_span)
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: guardrails if name == "traccia.guardrails" else fake,
    )
    adapter = TracciaObservabilityAdapter(TracciaSettings(enabled=True), "algen-agent-runtime-test")
    monkeypatch.setattr(adapter, "_register_otel_provider", lambda provider: None)

    with adapter.guardrail_scope(
        name="pii",
        category="pii",
        enforcement_mode="warn",
        policy_id="policy-1",
    ) as span:
        span.set_attribute("guardrail.triggered", True)

    assert (
        "pii",
        {
            "category": "pii",
            "enforcement_mode": "warn",
            "policy_id": "policy-1",
        },
    ) in calls


def test_minimal_tracing_emits_only_triggered_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []
    fake = SimpleNamespace(
        init=lambda **options: calls.append(("init", options)),
        runtime_config=FakeRuntimeConfig(calls),
        stop_tracing=lambda timeout: None,
    )

    @contextmanager
    def guardrail_span(name: str, **attributes: Any) -> Iterator[RecordedSpan]:
        calls.append((name, attributes))
        yield RecordedSpan(name=name, parent=None)

    guardrails = SimpleNamespace(guardrail_span=guardrail_span)
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: guardrails if name == "traccia.guardrails" else fake,
    )
    adapter = TracciaObservabilityAdapter(
        TracciaSettings(enabled=True), "algen-agent-runtime-test", "minimal"
    )
    monkeypatch.setattr(adapter, "_register_otel_provider", lambda provider: None)

    with adapter.guardrail_scope(
        name="secrets", category="input_validation", enforcement_mode="warn"
    ) as span:
        span.set_attribute("guardrail.triggered", False)
    assert not any(name == "secrets" for name, _ in calls)

    with adapter.guardrail_scope(
        name="secrets", category="input_validation", enforcement_mode="warn"
    ) as span:
        span.set_attribute("guardrail.triggered", True)
        span.set_attribute("guardrail.reason_code", "secret.detected")
    assert any(name == "secrets" for name, _ in calls)


def test_traccia_managed_root_receives_guardrail_summary() -> None:
    traccia = pytest.importorskip("traccia")
    detector_module = pytest.importorskip("traccia.processors.guardrail_detector")
    tracer_module = pytest.importorskip("traccia.tracer")
    original = traccia.get_tracer_provider()
    provider = tracer_module.TracerProvider()
    provider.add_span_processor(detector_module.GuardrailDetectorProcessor())
    traccia.set_tracer_provider(provider)
    adapter = TracciaObservabilityAdapter(TracciaSettings(enabled=True), "algen-agent-runtime-test")
    adapter._module = traccia
    adapter._started = True
    try:
        scope = adapter.span_scope(
            "conversation.turn",
            attributes={"span.type": "agent", "agent.id": "test-agent"},
            root=True,
        )
        assert scope is not None
        with scope as root:
            with adapter.guardrail_scope(
                name="pii", category="pii", enforcement_mode="warn"
            ) as guardrail:
                guardrail.set_attribute("guardrail.triggered", True)
        summary = json.loads(root.attributes["guardrail.summary"])
        assert summary["triggered_categories"] == ["pii"]
        assert summary["coverage_confidence"] == "high"
    finally:
        traccia.set_tracer_provider(original)
        adapter._started = False


def test_traccia_adapter_reports_missing_optional_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(name: str) -> None:
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module", missing
    )
    adapter = TracciaObservabilityAdapter(TracciaSettings(enabled=True), "algen-agent-runtime-test")
    with pytest.raises(ConfigurationError, match=r"\[traccia\]"):
        adapter.start()


def test_traccia_adapter_bridges_legacy_span_processors() -> None:
    endings: list[object] = []

    class Downstream:
        def _on_ending(self, span: object) -> None:
            endings.append(span)

    legacy = SimpleNamespace(next_processor=Downstream())
    provider = SimpleNamespace(
        _otel_tracer_provider=SimpleNamespace(
            _active_span_processor=SimpleNamespace(_span_processors=(legacy,))
        )
    )

    TracciaObservabilityAdapter._ensure_span_processor_compatibility(provider)
    marker = object()
    legacy._on_ending(marker)

    assert endings == [marker]


def test_traccia_adapter_requires_referenced_environment_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = SimpleNamespace(init=lambda **options: None)
    monkeypatch.delenv("MISSING_TRACCIA_KEY", raising=False)
    monkeypatch.setattr(
        "algen_agent_runtime.observability.traccia_adapter.importlib.import_module",
        lambda name: fake,
    )
    adapter = TracciaObservabilityAdapter(
        TracciaSettings(enabled=True, api_key="env://MISSING_TRACCIA_KEY"),
        "algen-agent-runtime-test",
    )
    with pytest.raises(ConfigurationError, match="MISSING_TRACCIA_KEY"):
        adapter.start()


async def test_runtime_scopes_run_with_agent_identity() -> None:
    calls: list[tuple[str, Any]] = []

    class RecordingObservability(NoopObservabilityAdapter):
        @contextmanager
        def run_scope(self, *, agent_id: str, agent_name: str, tenant_id: str) -> Iterator[None]:
            calls.append(
                (
                    "enter",
                    {
                        "agent_id": agent_id,
                        "agent_name": agent_name,
                        "tenant_id": tenant_id,
                    },
                )
            )
            yield
            calls.append(("exit", {}))

    runtime = make_runtime()
    runtime.observability = RecordingObservability()
    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="hello",
            tenant_id="tenant",
            user_id="user",
        )
    )

    assert result.output == "answer"
    assert calls == [
        (
            "enter",
            {
                "agent_id": "test-agent",
                "agent_name": "test-agent",
                "tenant_id": "tenant",
            },
        ),
        ("exit", {}),
    ]


@dataclass
class RecordedSpan:
    name: str
    parent: str | None
    attributes: dict[str, Any] = field(default_factory=dict)

    def set_attribute(self, name: str, value: Any) -> None:
        self.attributes[name] = value


class RecordingTracer:
    def __init__(self) -> None:
        self.spans: list[RecordedSpan] = []
        self._current: list[RecordedSpan] = []

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
        context: Any | None = None,
    ) -> Iterator[RecordedSpan]:
        span = RecordedSpan(
            name=name,
            parent=(
                None if context is not None else self._current[-1].name if self._current else None
            ),
            attributes=dict(attributes or {}),
        )
        self.spans.append(span)
        self._current.append(span)
        try:
            yield span
        finally:
            self._current.pop()


def test_trace_level_tracer_filters_internal_spans_by_configured_detail() -> None:
    recorded = RecordingTracer()
    minimal = TraceLevelTracer(recorded, "minimal")
    with minimal.start_as_current_span("agent.run"):
        with minimal.start_as_current_span("agent.planning"):
            pass
        with minimal.start_as_current_span("agent.model.call"):
            pass
        with minimal.start_as_current_span("agent.policy.input"):
            pass
        with minimal.start_as_current_span("cache.get"):
            pass
    assert [span.name for span in recorded.spans] == ["agent.run", "agent.model.call"]

    recorded = RecordingTracer()
    standard = TraceLevelTracer(recorded, "standard")
    with standard.start_as_current_span("agent.policy.input"):
        pass
    with standard.start_as_current_span("agent.verification"):
        pass
    with standard.start_as_current_span("agent.step"):
        pass
    assert [span.name for span in recorded.spans] == [
        "agent.policy.input",
        "agent.verification",
    ]


async def test_runtime_emits_one_root_trace_with_traccia_semantics() -> None:
    class EnvironmentObservability(NoopObservabilityAdapter):
        def span_attributes(self) -> dict[str, str]:
            return {"env": "production", "environment": "production"}

    runtime = make_runtime()
    runtime.observability = EnvironmentObservability()
    tracer = RecordingTracer()
    runtime._tracer = tracer

    result = await runtime.run(
        RunRequest(
            agent="test-agent",
            input="hello",
            tenant_id="tenant",
            user_id="user",
        )
    )

    assert result.output == "answer"
    roots = [span for span in tracer.spans if span.parent is None]
    assert [span.name for span in roots] == ["agent.run"]
    assert all(span.name == "agent.run" or span.parent is not None for span in tracer.spans)

    root = roots[0]
    assert root.attributes["agent.id"] == "test-agent"
    assert root.attributes["agent.name"] == "test-agent"
    assert root.attributes["agent.version"] == "1.0.0"
    assert root.attributes["agent.definition.id"] == "test-agent@1.0.0"
    assert root.attributes["gen_ai.agent.id"] == "test-agent"
    assert root.attributes["gen_ai.agent.version"] == "1.0.0"
    assert root.attributes["agent.description"] == "deterministic test agent"
    assert root.attributes["session.id"]
    assert root.attributes["environment"] == "production"
    assert root.attributes["agent.run.status"] == "completed"
    assert root.attributes["agent.run.model_calls"] == 1

    assert {
        span.attributes["agent.id"] for span in tracer.spans if "agent.id" in span.attributes
    } == {"test-agent"}
    assert {
        span.attributes["agent.definition.id"]
        for span in tracer.spans
        if "agent.definition.id" in span.attributes
    } == {"test-agent@1.0.0"}

    planning_span = next(span for span in tracer.spans if span.name == "agent.planning")
    assert planning_span.attributes["agent.planner.name"] == "react"
    step_span = next(span for span in tracer.spans if span.name == "agent.step")
    assert step_span.attributes["agent.step.type"] == "model"
    model_span_parent = next(
        span.parent for span in tracer.spans if span.name == "agent.model.call"
    )
    assert model_span_parent == "agent.step"

    policy_span = next(span for span in tracer.spans if span.name == "agent.policy.input")
    assert "guardrail.name" not in policy_span.attributes
    assert policy_span.attributes["span.type"] == "policy_pipeline"
    assert policy_span.attributes["policy.operation"] == "agent.policy.evaluate"
    assert policy_span.attributes["policy.boundary"] == "input"
    assert policy_span.attributes["policy.subject_type"] == "str"
    assert policy_span.attributes["policy.invoked_count"] >= 1
    assert policy_span.attributes["policy.triggered_count"] == 0
    assert policy_span.attributes["policy.source_sdk"] == "algen_agent_runtime"
    assert policy_span.attributes["policy.action"] == "allow"

    model_span = next(span for span in tracer.spans if span.name == "agent.model.call")
    assert model_span.attributes["span.type"] == "LLM"
    assert model_span.attributes["llm.vendor"] == "mock"
    assert model_span.attributes["llm.usage.prompt_tokens"] == 10
    assert model_span.attributes["llm.usage.total_tokens"] == 11
    assert model_span.attributes["llm.finish_reason"] == "stop"
    assert model_span.attributes["llm.cost.source"] == "configured_provider_rates"
    assert "llm.prompt" not in model_span.attributes


async def test_runtime_content_telemetry_is_opt_in_redacted_and_bounded() -> None:
    runtime = make_runtime()
    runtime.telemetry_include_content = True
    runtime.telemetry_max_content_chars = 32
    tracer = RecordingTracer()
    runtime._tracer = tracer

    await runtime.run(
        RunRequest(
            agent="test-agent",
            input="Bearer highly-sensitive-token-value that must not be exported",
            tenant_id="tenant",
            user_id="user",
        )
    )

    model_span = next(span for span in tracer.spans if span.name == "agent.model.call")
    prompt = model_span.attributes["llm.prompt"]
    assert "highly-sensitive-token-value" not in prompt
    assert prompt.endswith("...[TRUNCATED]")
    assert "llm.completion" in model_span.attributes


async def test_conversation_turn_is_one_trace_with_agent_runs_as_children() -> None:
    runtime = make_runtime()
    tracer = RecordingTracer()
    runtime._tracer = tracer
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    conversations = ConversationService(
        InMemoryConversationStore(),
        InMemoryConversationEventBus(),
        handlers,
        telemetry_include_content=True,
    )
    conversations._tracer = tracer
    conversation = await conversations.create(
        tenant_id="tenant-a",
        user_id="user-a",
        agent="test-agent",
    )
    user_message, _ = await conversations.submit(
        conversation.id,
        "tenant-a",
        "user-a",
        "Show route performance",
    )
    for _ in range(100):
        messages = await conversations.messages(conversation.id, "tenant-a")
        if messages[-1].status.value == "completed":
            break
        await asyncio.sleep(0.001)

    turn = next(span for span in tracer.spans if span.name == "conversation.turn")
    agent_run = next(span for span in tracer.spans if span.name == "agent.run")
    assert turn.parent is None
    assert agent_run.parent == "conversation.turn"
    assert turn.attributes["session.id"] == conversation.id
    assert turn.attributes["gen_ai.conversation.id"] == conversation.id
    assert turn.attributes["interaction.id"] == user_message.id
    assert turn.attributes["conversation.input"] == "Show route performance"
    assert turn.attributes["conversation.output"] == "answer"
    assert turn.attributes["conversation.turn.status"] == "completed"
    assert turn.attributes["conversation.agent_run_count"] == 1
