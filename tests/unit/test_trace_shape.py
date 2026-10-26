from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.observability.traccia_adapter import NoopObservabilityAdapter
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest, RunStatus


class RecordingAdapter(NoopObservabilityAdapter):
    """Sends every Runtime span, guardrails included, to an in-memory exporter."""

    def __init__(self, trace_level: Any) -> None:
        super().__init__(trace_level)
        self.exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self.exporter))

    def tracer(self, name: str) -> Any:
        return self._provider.get_tracer(name)


async def lookup(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"answer": 42}


def _tool(side_effect: SideEffect) -> Tool:
    return Tool(
        ToolDefinition(
            name="kb.lookup",
            version="1",
            description="Look up.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            side_effect=side_effect,
        ),
        lookup,
    )


def _container(
    monkeypatch: pytest.MonkeyPatch, level: str, side_effect: SideEffect = SideEffect.READ
) -> tuple[Container, RecordingAdapter]:
    adapter = RecordingAdapter(level)
    monkeypatch.setattr(
        "algen_agent_runtime.orchestration.container.observability_adapter",
        lambda settings, environment=None: adapter,
    )
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "m"}},
            "agents": [
                {
                    "name": "traced",
                    "version": "1.0.0",
                    "description": "d",
                    "system_instructions": "x",
                    "default_model": {"name": "m", "provider": "mock", "model": "m"},
                    "enabled_tools": ["kb.lookup"],
                    "tool_permissions": ["kb.write"] if side_effect != SideEffect.READ else [],
                }
            ],
            "telemetry": {"trace_level": level},
            "runtime": {"recover_incomplete_runs": False},
        }
    )
    container = build_container(settings, additional_tools=(_tool(side_effect),))
    model = container.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    model.queue(tool_call_response("kb.lookup", {}), "final answer")
    return container, adapter


def _request() -> RunRequest:
    return RunRequest(agent="traced", input="q", tenant_id="t", user_id="u")


def _attrs(span: ReadableSpan) -> dict[str, Any]:
    return dict(span.attributes or {})


def _spans(adapter: RecordingAdapter) -> list[ReadableSpan]:
    return list(adapter.exporter.get_finished_spans())


async def test_every_runtime_span_is_typed_and_in_one_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, adapter = _container(monkeypatch, "detailed")

    result = await AlgenAgentRuntimeClient(container.runtime).run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    spans = _spans(adapter)
    untyped = sorted({span.name for span in spans if not (span.attributes or {}).get("span.type")})
    assert untyped == []
    assert len({span.context.trace_id for span in spans if span.context}) == 1
    names = [span.name for span in spans]
    assert names.count("agent.model.call") == 2
    assert names.count("agent.tool.call") == 1


async def test_minimal_keeps_the_decision_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    container, adapter = _container(monkeypatch, "minimal")

    await AlgenAgentRuntimeClient(container.runtime).run(_request())

    names = {span.name for span in _spans(adapter)}
    assert {
        "agent.run",
        "agent.step",
        "agent.model.call",
        "agent.tool.call",
        "agent.policy.before_model",
        "agent.policy.before_tool",
        "agent.verification",
    } <= names
    # Planning, context, memory and unfired guardrails are left to the richer levels.
    assert not names & {"agent.planning", "agent.context.build", "agent.memory.write"}
    assert not any(name.startswith(("guardrail.", "cache.")) for name in names)


async def test_a_resumed_run_continues_its_first_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    container, adapter = _container(monkeypatch, "minimal", SideEffect.WRITE)
    runtime = container.runtime

    paused = await AlgenAgentRuntimeClient(runtime).run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    finished = await runtime.wait(paused.run_id, "t")

    assert finished.status == RunStatus.COMPLETED, finished.error
    state = await runtime.runs.get(paused.run_id, "t")
    assert state is not None and state.trace_parent is not None
    roots = [span for span in _spans(adapter) if span.name == "agent.run"]
    assert len(roots) == 2
    first, second = roots
    assert first.context and second.context and second.parent
    assert second.context.trace_id == first.context.trace_id
    assert second.parent.span_id == first.context.span_id
    assert state.trace_parent == {
        "trace_id": format(first.context.trace_id, "032x"),
        "span_id": format(first.context.span_id, "016x"),
    }


def test_usage_attributes_split_the_cache_and_price_only_priced_calls() -> None:
    from algen_agent_runtime.runtime.runtime import _usage_attributes
    from algen_agent_runtime.types.contracts import TokenUsage

    unpriced = _usage_attributes(
        TokenUsage(input_tokens=9_250, output_tokens=7, cached_tokens=9_000, cache_write_tokens=200)
    )
    assert unpriced == {
        "llm.usage.prompt_tokens": 50,
        "llm.usage.completion_tokens": 7,
        "llm.usage.cache_read_tokens": 9_000,
        "llm.usage.cache_write_tokens": 200,
        "llm.usage.total_tokens": 9_257,
        "llm.usage.source": "provider_usage",
        "gen_ai.usage.input_tokens": 9_250,
        "gen_ai.usage.output_tokens": 7,
        "gen_ai.usage.cache_read.input_tokens": 9_000,
        "gen_ai.usage.cache_creation.input_tokens": 200,
    }
    priced = _usage_attributes(
        TokenUsage(input_tokens=10, output_tokens=1, estimated_cost_usd=0.02)
    )
    assert (priced["llm.cost.usd"], priced["llm.cost.source"]) == (
        0.02,
        "configured_provider_rates",
    )


async def test_spans_carry_safe_analysis_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    container, adapter = _container(monkeypatch, "minimal")

    await AlgenAgentRuntimeClient(container.runtime).run(
        RunRequest(agent="traced", input="find it", tenant_id="t", user_id="u")
    )

    spans = _spans(adapter)
    root = next(span for span in spans if span.name == "agent.run")
    first_model, second_model = (span for span in spans if span.name == "agent.model.call")
    tool = next(span for span in spans if span.name == "agent.tool.call")
    assert root.attributes and first_model.attributes and second_model.attributes
    assert tool.attributes
    assert root.attributes["agent.description"] == "d"
    assert root.attributes["agent.run.input_chars"] == len("find it")
    assert root.attributes["agent.run.output_chars"] == len("final answer")
    assert "agent.run.estimated_cost_usd" not in root.attributes
    assert "agent.tags" not in root.attributes  # empty values are left out
    assert all(
        "agent.description" not in (span.attributes or {}) for span in spans if span is not root
    )
    assert first_model.attributes["llm.response.tool_names"] == ("kb.lookup",)
    assert first_model.attributes["llm.request.tool_count"] == 1
    assert _attrs(first_model)["agent.model.prepare_ms"] >= 0
    assert second_model.attributes["llm.response.output_chars"] == len("final answer")
    assert tool.attributes["gen_ai.tool.name"] == "kb.lookup"
    assert tool.attributes["tool.argument_keys"] == ()
    assert _attrs(tool)["tool.result_chars"] > 0


async def test_streamed_model_calls_record_time_to_first_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, adapter = _container(monkeypatch, "minimal")

    async for _ in AlgenAgentRuntimeClient(container.runtime).stream(
        RunRequest(agent="traced", input="q", tenant_id="t", user_id="u", stream=True)
    ):
        pass

    models = [span for span in _spans(adapter) if span.name == "agent.model.call"]
    assert models and all(
        _attrs(span).get("llm.time_to_first_token_ms", -1) >= 0 for span in models
    )


async def test_the_narrative_tells_a_run_in_order_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, _ = _container(monkeypatch, "minimal")
    runtime = container.runtime
    result = await AlgenAgentRuntimeClient(runtime).run(
        RunRequest(agent="traced", input="secret question", tenant_id="t", user_id="u")
    )

    narrative = await runtime.narrative(result.run_id, "t")

    assert (narrative.status, narrative.model_calls, narrative.tool_calls) == ("completed", 2, 1)
    assert narrative.estimated_cost_usd is None  # unpriced mock provider
    assert narrative.trace_id is not None and len(narrative.trace_id) == 32  # links to the trace
    summaries = [entry.summary for entry in narrative.entries]
    kinds = [entry.kind for entry in narrative.entries]
    assert kinds[0] == "run" and kinds[-1] == "outcome"
    assert kinds.index("tool") > kinds.index("model")
    assert any("requested tools: kb.lookup" in summary for summary in summaries)
    assert "Called kb.lookup with arguments: none" in summaries
    assert summaries[-1] == "Run completed"
    exported = narrative.model_dump_json()
    assert "secret question" not in exported and "final answer" not in exported
    assert narrative.input_chars == len("secret question")
    assert narrative.output_chars == len("final answer")


async def test_the_narrative_shows_an_approval_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    container, _ = _container(monkeypatch, "minimal", SideEffect.WRITE)
    runtime = container.runtime
    paused = await AlgenAgentRuntimeClient(runtime).run(_request())

    waiting = await runtime.narrative(paused.run_id, "t")
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    await runtime.wait(paused.run_id, "t")
    finished = await runtime.narrative(paused.run_id, "t")

    assert waiting.status == "awaiting_approval"
    assert waiting.entries[-1].kind == "approval"
    assert finished.status == "completed"
    assert "approval" in [entry.kind for entry in finished.entries]
