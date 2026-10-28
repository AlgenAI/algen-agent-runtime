from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime import MeterRefusedError, UsageMeter
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.artifacts import CONTEXT_READ_TOOL, ContextArtifactService
from algen_agent_runtime.context.budget import estimate_request_tokens
from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.observability.traccia_adapter import NoopObservabilityAdapter
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ContextPolicy,
    ErrorKind,
    FinishReason,
    Message,
    ModelProfile,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    RequestOverrides,
    RetryPolicy,
    Role,
    RunRequest,
    RunStatus,
    TokenUsage,
    ToolPolicy,
)


class RecordingMeter:
    """Records every reservation; refuses from the ``refuse_from``-th call on."""

    def __init__(self, refuse_from: int | None = None, reason: str = "No credits left.") -> None:
        self.refuse_from = refuse_from
        self.reason = reason
        self.attempts = 0
        self.reserved: list[tuple[int, int, int]] = []
        self.settled: list[tuple[int, TokenUsage]] = []
        self.released: list[int] = []

    async def reserve(self, *, max_input_tokens: int, max_output_tokens: int) -> object:
        self.attempts += 1
        if self.refuse_from is not None and self.attempts >= self.refuse_from:
            raise MeterRefusedError(self.reason)
        ticket = len(self.reserved)
        self.reserved.append((ticket, max_input_tokens, max_output_tokens))
        return ticket

    async def settle(self, ticket: object, usage: TokenUsage) -> None:
        assert isinstance(ticket, int)
        self.settled.append((ticket, usage))

    async def release(self, ticket: object) -> None:
        assert isinstance(ticket, int)
        self.released.append(ticket)

    @property
    def open(self) -> set[int]:
        closed = {ticket for ticket, _ in self.settled} | set(self.released)
        return {ticket for ticket, _, _ in self.reserved} - closed


def metered(
    meter: RecordingMeter,
    *providers: tuple[str, MockModelProvider],
    agent: AgentDefinition | None = None,
    **kwargs: Any,
) -> AgentRuntime:
    runtime = make_runtime(providers[0][1], agent, **kwargs)
    router = ModelRouter(circuit_failure_threshold=1, usage_meter=meter)
    for registration_id, provider in providers:
        router.register_provider(provider, registration_id=registration_id)
    runtime.router = router
    return runtime


def request(**updates: Any) -> RunRequest:
    return RunRequest(agent="test-agent", input="hello", tenant_id="t", user_id="u", **updates)


class FailingProvider(MockModelProvider):
    """Fails its first ``failures`` calls with a provider error, then answers."""

    def __init__(self, *responses: str, failures: int, retryable: bool = True) -> None:
        super().__init__(responses)
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise ProviderError("provider unavailable", ErrorKind.UNAVAILABLE, self.retryable)
        return await super().generate(request)


async def lookup(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"answer": 42}


def lookup_tool(name: str = "kb.lookup", side_effect: SideEffect = SideEffect.READ) -> Tool:
    return Tool(
        ToolDefinition(
            name=name,
            version="1",
            description="Look up.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            side_effect=side_effect,
        ),
        lookup,
    )


def assert_balanced(meter: RecordingMeter) -> None:
    assert meter.open == set()
    assert not {ticket for ticket, _ in meter.settled} & set(meter.released)


async def test_every_step_reserves_the_router_estimate_and_settles_reported_usage() -> None:
    meter = RecordingMeter()
    provider = MockModelProvider(["answer"])
    runtime = metered(meter, ("mock", provider))

    result = await runtime.run(request())

    assert result.status == RunStatus.COMPLETED, result.error
    sent = provider.requests[0]
    assert meter.reserved == [(0, estimate_request_tokens(sent), sent.max_output_tokens)]
    assert [ticket for ticket, _ in meter.settled] == [0]
    assert meter.settled[0][1].total_tokens == result.execution_summary.usage.total_tokens
    assert meter.released == []


async def test_refusal_ends_the_run_without_retry_fallback_or_provider_blame() -> None:
    meter = RecordingMeter(refuse_from=1, reason="Monthly model allowance used up.")
    primary, fallback = MockModelProvider(["a"]), MockModelProvider(["b"])
    agent = make_agent(
        fallback_models=(ModelProfile(name="b", provider="backup", model="deterministic"),),
        retry_policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=0),
    )
    runtime = metered(meter, ("mock", primary), ("backup", fallback), agent=agent)

    result = await runtime.run(request())

    assert result.status == RunStatus.FAILED
    assert result.error == "Monthly model allowance used up."
    assert meter.attempts == 1  # neither retried nor passed to the fallback
    assert primary.requests == [] and fallback.requests == []
    assert result.execution_summary.retries == 0
    assert all(health.consecutive_failures == 0 for health in runtime.router._health.values())
    assert all(health.opened_at is None for health in runtime.router._health.values())
    history = await runtime.events.history(result.run_id)
    types = [event.type for event in history]
    assert "model.retrying" not in types
    refused = next(event for event in history if event.type == "budget.refused")
    assert refused.data == {"reason": "Monthly model allowance used up."}
    assert history[-1].type == "run.failed"
    assert history[-1].data == {
        "error": "Monthly model allowance used up.",
        "reason": "budget_refused",
    }
    audits = await runtime.audits.list("t")
    assert any(audit.action == "budget.refused" for audit in audits)
    narrative = await runtime.narrative(result.run_id, "t")
    assert any("Usage meter refused" in entry.summary for entry in narrative.entries)


async def test_refusal_after_a_tool_round_keeps_the_charged_call() -> None:
    meter = RecordingMeter(refuse_from=2)
    provider = MockModelProvider([tool_call_response("kb.lookup", {}), "final"])
    agent = make_agent(enabled_tools={"kb.lookup"})
    runtime = metered(meter, ("mock", provider), agent=agent)
    runtime.tools.register(lookup_tool())

    result = await runtime.run(request())

    assert result.status == RunStatus.FAILED
    assert result.error == "No credits left."
    assert result.execution_summary.model_calls == 1
    assert len(meter.settled) == 1
    assert_balanced(meter)


async def test_retry_releases_the_failed_attempt_and_settles_the_retry() -> None:
    meter = RecordingMeter()
    provider = FailingProvider("answer", failures=1)
    agent = make_agent(retry_policy=RetryPolicy(max_attempts=2, initial_backoff_seconds=0))
    runtime = metered(meter, ("mock", provider), agent=agent)
    # The circuit opens after one failure in this router; reopen it so the retry is dispatched.
    runtime.router._reset_seconds = 0

    result = await runtime.run(request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert result.execution_summary.retries == 1
    assert len(meter.reserved) == 2
    assert meter.released == [0]
    assert [ticket for ticket, _ in meter.settled] == [1]
    assert_balanced(meter)


async def test_truncated_completion_retry_settles_both_calls() -> None:
    truncated = ModelResponse(
        message=Message.text(Role.ASSISTANT, "partial"),
        finish_reason=FinishReason.LENGTH,
        usage=TokenUsage(input_tokens=100, output_tokens=700),
        model="deterministic",
        provider="mock",
    )
    meter = RecordingMeter()
    provider = MockModelProvider([truncated, "complete"])
    runtime = metered(meter, ("mock", provider))

    result = await runtime.run(request())

    assert result.output == "complete"
    # The truncated reply was delivered and used tokens, so it is charged, not released.
    assert [ticket for ticket, _ in meter.settled] == [0, 1]
    assert meter.settled[0][1].output_tokens == 700
    assert meter.reserved[1][2] == provider.requests[1].max_output_tokens
    assert meter.released == []


async def test_fallback_releases_the_failed_candidate_and_settles_the_next() -> None:
    meter = RecordingMeter()
    primary = FailingProvider("never", failures=1, retryable=False)
    fallback = MockModelProvider(["from fallback"])
    agent = make_agent(
        fallback_models=(ModelProfile(name="b", provider="backup", model="deterministic"),)
    )
    runtime = metered(meter, ("mock", primary), ("backup", fallback), agent=agent)

    result = await runtime.run(request())

    assert result.output == "from fallback"
    assert meter.released == [0]
    assert [ticket for ticket, _ in meter.settled] == [1]
    assert_balanced(meter)


async def test_failed_call_is_released_without_charge() -> None:
    meter = RecordingMeter()
    provider = FailingProvider("never", failures=5, retryable=False)
    runtime = metered(meter, ("mock", provider))

    result = await runtime.run(request())

    assert result.status == RunStatus.FAILED
    assert meter.settled == []
    assert meter.released == [0]


async def test_schema_repair_is_metered_like_any_step() -> None:
    meter = RecordingMeter()
    provider = MockModelProvider(["not-json", "{}"])
    runtime = metered(meter, ("mock", provider))
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    result = await runtime.run(request(overrides=RequestOverrides(response_schema=schema)))

    assert result.output == "{}"
    assert len(provider.requests) == 2
    assert [ticket for ticket, _ in meter.settled] == [0, 1]
    assert_balanced(meter)


async def test_streamed_call_settles_on_completion() -> None:
    meter = RecordingMeter()
    provider = MockModelProvider(["one two three"])
    runtime = metered(meter, ("mock", provider))

    result = await runtime.run(request(stream=True))

    assert result.status == RunStatus.COMPLETED, result.error
    assert provider.requests[0].stream is True
    assert [ticket for ticket, _ in meter.settled] == [0]
    assert meter.released == []


class BrokenStreamProvider(MockModelProvider):
    """Delivers one delta, then fails or waits forever."""

    def __init__(self, *, hang: bool = False) -> None:
        super().__init__(["unused"])
        self.hang = hang
        self.started = asyncio.Event()

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        yield ModelStreamEvent(type="delta", delta="partial ")
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        raise ProviderError("connection reset", ErrorKind.UNAVAILABLE, retryable=False)


async def test_stream_error_releases_the_reservation() -> None:
    meter = RecordingMeter()
    runtime = metered(meter, ("mock", BrokenStreamProvider()))

    result = await runtime.run(request(stream=True))

    assert result.status == RunStatus.FAILED
    assert meter.settled == []
    assert meter.released == [0]


async def test_cancelled_stream_releases_the_reservation() -> None:
    meter = RecordingMeter()
    provider = BrokenStreamProvider(hang=True)
    runtime = metered(meter, ("mock", provider))

    state = await runtime.start(request(stream=True))
    await asyncio.wait_for(provider.started.wait(), 5)
    await runtime.cancel(state.id, "t")
    result = await runtime.wait(state.id, "t")

    assert result.status == RunStatus.CANCELLED
    assert meter.settled == []
    assert meter.released == [0]


async def test_stream_closed_early_by_its_consumer_is_released() -> None:
    meter = RecordingMeter()
    router = ModelRouter(usage_meter=meter)
    router.register_provider(MockModelProvider(["one two three"]))
    profile = ModelProfile(name="m", provider="mock", model="deterministic")
    stream = router.stream(
        ModelRequest(messages=(Message.text(Role.USER, "hi"),), max_output_tokens=64), (profile,)
    )

    first = await anext(stream)
    await stream.aclose()

    assert first.delta == "one "
    assert meter.reserved[0][2] == 64
    assert meter.settled == [] and meter.released == [0]


async def test_stream_refusal_happens_before_the_provider_is_called() -> None:
    meter = RecordingMeter(refuse_from=1)
    provider = MockModelProvider(["one"])
    runtime = metered(meter, ("mock", provider))

    result = await runtime.run(request(stream=True))

    assert result.status == RunStatus.FAILED
    assert result.error == "No credits left."
    assert provider.requests == []


def compaction_agent() -> AgentDefinition:
    return make_agent(
        enabled_tools={CONTEXT_READ_TOOL},
        tool_permissions={"context.read"},
        context_policy=ContextPolicy(
            compact_history=True,
            compaction_trigger_tokens=256,
            compaction_keep_recent_groups=2,
            compaction_max_output_tokens=128,
        ),
    )


async def compaction_run(meter: RecordingMeter, provider: MockModelProvider) -> Any:
    runtime = metered(
        meter,
        ("mock", provider),
        agent=compaction_agent(),
        context_artifacts=ContextArtifactService(InMemoryArtifactStore()),
    )
    await runtime.memory.append(
        "tenant",
        "session",
        [
            Message.text(Role.USER, "Old goal " * 150),
            Message.text(Role.ASSISTANT, "Old work " * 150),
        ],
    )
    return await runtime.run(
        RunRequest(
            agent="test-agent",
            input="New request",
            session_id="session",
            tenant_id="tenant",
            user_id="user",
        )
    )


async def test_compaction_call_is_metered() -> None:
    meter = RecordingMeter()
    provider = MockModelProvider(["Earlier work summarized.", "Answer."])

    result = await compaction_run(meter, provider)

    assert result.status == RunStatus.COMPLETED, result.error
    assert len(meter.reserved) == 2
    summary_request = provider.requests[0]
    assert meter.reserved[0][1:] == (estimate_request_tokens(summary_request), 128)
    assert [ticket for ticket, _ in meter.settled] == [0, 1]


async def test_refused_compaction_ends_the_run() -> None:
    meter = RecordingMeter(refuse_from=1)
    provider = MockModelProvider(["Earlier work summarized.", "Answer."])

    result = await compaction_run(meter, provider)

    assert result.status == RunStatus.FAILED
    assert result.error == "No credits left."
    assert provider.requests == []
    assert result.artifacts == ()


async def test_parallel_tool_batch_and_concurrent_runs_balance_the_meter() -> None:
    meter = RecordingMeter()

    def handler(model_request: ModelRequest) -> ModelResponse:
        if model_request.messages[-1].role == Role.TOOL:
            return ModelResponse(
                message=Message.text(Role.ASSISTANT, "done"),
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(input_tokens=5, output_tokens=1),
                model="deterministic",
                provider="mock",
            )
        first = tool_call_response("kb.a", {})
        second = tool_call_response("kb.b", {})
        return first.model_copy(update={"tool_calls": (*first.tool_calls, *second.tool_calls)})

    agent = make_agent(enabled_tools={"kb.a", "kb.b"}, tool_policy=ToolPolicy(parallel_calls=True))
    runtime = metered(meter, ("mock", MockModelProvider(handler=handler)), agent=agent)
    runtime.tools.register(lookup_tool("kb.a"))
    runtime.tools.register(lookup_tool("kb.b"))

    results = await asyncio.gather(*(runtime.run(request()) for _ in range(3)))

    assert all(result.output == "done" for result in results)
    assert sum(result.execution_summary.model_calls for result in results) == 6
    assert len(meter.settled) == 6
    assert_balanced(meter)


class RecordingAdapter(NoopObservabilityAdapter):
    def __init__(self, trace_level: Any) -> None:
        super().__init__(trace_level)
        self.exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def tracer(self, name: str) -> Any:
        return self._provider.get_tracer(name)


def container(
    monkeypatch: pytest.MonkeyPatch, meter: UsageMeter
) -> tuple[Container, RecordingAdapter]:
    adapter = RecordingAdapter("minimal")
    monkeypatch.setattr(
        "algen_agent_runtime.orchestration.container.observability_adapter",
        lambda settings, environment=None: adapter,
    )
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "m"}},
            "agents": [
                {
                    "name": "metered",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "mock", "model": "m"},
                    "enabled_tools": ["kb.write"],
                    "tool_permissions": ["kb.write"],
                }
            ],
            "runtime": {"recover_incomplete_runs": False},
        }
    )
    built = build_container(
        settings,
        additional_tools=(lookup_tool("kb.write", SideEffect.WRITE),),
        usage_meter=meter,
    )
    model = built.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    model.queue(tool_call_response("kb.write", {}), "final answer")
    return built, adapter


def metered_request() -> RunRequest:
    return RunRequest(agent="metered", input="q", tenant_id="t", user_id="u")


async def test_build_container_meters_across_an_approval_pause_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meter = RecordingMeter()
    built, _ = container(monkeypatch, meter)
    runtime = built.runtime

    paused = await AlgenAgentRuntimeClient(runtime).run(metered_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert len(meter.settled) == 1

    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    finished = await runtime.wait(paused.run_id, "t")

    assert finished.status == RunStatus.COMPLETED, finished.error
    assert finished.output == "final answer"
    assert len(meter.settled) == 2
    assert_balanced(meter)


async def test_refusal_after_resume_fails_the_run_and_marks_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meter = RecordingMeter(refuse_from=2, reason="Credit limit reached.")
    built, adapter = container(monkeypatch, meter)
    runtime = built.runtime

    paused = await AlgenAgentRuntimeClient(runtime).run(metered_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    finished = await runtime.wait(paused.run_id, "t")

    assert finished.status == RunStatus.FAILED
    assert finished.error == "Credit limit reached."
    state = await runtime.runs.get(paused.run_id, "t")
    assert state is not None and state.error == "Credit limit reached."
    roots = [span for span in adapter.exporter.get_finished_spans() if span.name == "agent.run"]
    assert dict(roots[-1].attributes or {}).get("budget.refused") is True
    assert all("budget.refused" not in dict(span.attributes or {}) for span in roots[:-1])


async def test_without_a_meter_the_router_dispatches_as_before() -> None:
    provider = MockModelProvider(["answer"])
    runtime = make_runtime(provider)

    result = await runtime.run(request(stream=True))

    assert result.status == RunStatus.COMPLETED
    assert runtime.router._usage_meter is None
