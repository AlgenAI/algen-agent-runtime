"""Tool-call guards ported from the Algen Agent Harness, each off by default but the schema check.

Prechecks and the schema check refuse a call before it runs or anyone is asked; refusal limits
tell a model that keeps failing to stop; the repeat guard and per-tool call limits hold back
calls that cannot help; the wrap-up notice leaves the run room to answer. Their state lives in
``RunState``, so it survives an approval pause.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from conftest import make_agent, make_runtime
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.check import check_project
from algen_agent_runtime.orchestration.describe import describe_components
from algen_agent_runtime.runtime.guards import (
    FIX_IT,
    REPEATED,
    STOP,
    over_limit,
    tool_calls_left,
    wrap_up_note,
)
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.authoring import tool as typed_tool
from algen_agent_runtime.tools.authoring import write
from algen_agent_runtime.tools.contracts import EarlierCall, SideEffect, Tool, ToolDefinition
from algen_agent_runtime.tools.starter.clock import clock_tool
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ApprovalPolicy,
    Budget,
    ContinuationPolicy,
    FinishReason,
    Message,
    ModelResponse,
    RetryPolicy,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    TokenUsage,
    ToolCall,
    ToolPolicy,
    WrapUpPolicy,
)

RETURN = ToolPolicy(on_error="return_to_model")
NO_APPROVAL = ApprovalPolicy(require_for_side_effects=False, require_for_destructive=False)
_IDS = itertools.count()


def _tool(
    name: str,
    handler: Any,
    *,
    side_effect: SideEffect = SideEffect.READ,
    precheck: Any = None,
    volatile: bool = False,
) -> Tool:
    return Tool(
        ToolDefinition(
            name=name,
            version="1",
            description=name,
            side_effect=side_effect,
            volatile=volatile,
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
        ),
        handler,
        precheck=precheck,
    )


def _calls(*calls: tuple[str, dict[str, Any]], tokens: int = 0) -> ModelResponse:
    """One model reply asking for ``calls``, in order."""
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=tuple(
            ToolCall(id=f"call-{next(_IDS)}", name=name, arguments=arguments)
            for name, arguments in calls
        ),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(input_tokens=tokens),
        model="deterministic",
        provider="mock",
    )


def _call(name: str, arguments: dict[str, Any] | None = None, tokens: int = 0) -> ModelResponse:
    if not tokens:
        return tool_call_response(name, arguments or {})
    return _calls((name, arguments or {}), tokens=tokens)


def _results(provider: MockModelProvider) -> list[dict[str, Any]]:
    """Each tool result the model was shown in its last request, decoded from its first block."""
    return [
        json.loads(message.content[0].text)  # type: ignore[union-attr]
        for message in provider.requests[-1].messages
        if message.role == Role.TOOL
    ]


def _tool_messages(provider: MockModelProvider) -> list[Message]:
    return [message for message in provider.requests[-1].messages if message.role == Role.TOOL]


def _request(text: str = "go") -> RunRequest:
    return RunRequest(agent="test-agent", input=text, tenant_id="t", user_id="u")


async def _settle(runtime: AgentRuntime, run_id: str) -> RunState:
    for _ in range(500):
        state = await runtime.status(run_id, "t")
        if state.status not in {
            RunStatus.PLANNING,
            RunStatus.BUILDING_CONTEXT,
            RunStatus.INVOKING_MODEL,
            RunStatus.INVOKING_TOOL,
            RunStatus.VERIFYING,
            RunStatus.COMPOSING,
        }:
            return state
        await asyncio.sleep(0.001)
    raise AssertionError("run did not settle")


async def _event_types(runtime: AgentRuntime, run_id: str) -> list[str]:
    return [event.type for event in await runtime.events.history(run_id)]


def _runtime(
    provider: MockModelProvider, *tools: Tool, **agent: Any
) -> tuple[AgentRuntime, AgentDefinition]:
    agent.setdefault("max_steps", 30)
    definition = make_agent(
        enabled_tools=frozenset(tool.definition.name for tool in tools), **agent
    )
    runtime = make_runtime(provider, definition)
    for tool in tools:
        runtime.tools.register(tool)
    return runtime, definition


class Recorder:
    """A tool handler that records its calls, and a precheck that refuses what it is told to."""

    def __init__(self, refuse: Mapping[str, str] | None = None) -> None:
        self.ran: list[dict[str, Any]] = []
        self.checked: list[tuple[str, dict[str, Any], list[tuple[str, dict[str, Any]]]]] = []
        self.refuse = dict(refuse or {})

    def handler(self, arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        self.ran.append(arguments)
        return {"ok": True, "key": arguments.get("key")}

    async def precheck(
        self,
        definition: ToolDefinition,
        arguments: Mapping[str, Any],
        earlier: Sequence[EarlierCall],
    ) -> str | None:
        self.checked.append(
            (
                definition.name,
                dict(arguments),
                [(call.name, dict(args)) for call, args in earlier],
            )
        )
        return self.refuse.get(str(arguments.get("key")))


# 1. Checks before a side effect


async def test_a_call_its_precheck_passes_runs_and_is_put_to_the_user_as_before() -> None:
    recorder = Recorder()
    provider = MockModelProvider([_call("config.change", {"key": "a"}), "changed"])
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
    )
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert recorder.checked == [("config.change", {"key": "a"}, [])]
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    state = await _settle(runtime, paused.run_id)
    assert state.status == RunStatus.COMPLETED and recorder.ran == [{"key": "a"}]
    # Approved calls passed their precheck when they were planned; it does not run again.
    assert len(recorder.checked) == 1
    assert state.tool_refusals == {}


async def test_a_refused_call_never_runs_and_nobody_is_asked() -> None:
    recorder = Recorder(refuse={"bad": "host example.org is not allowed"})
    provider = MockModelProvider([_call("config.change", {"key": "bad"}), "I could not."])
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
    )
    result = await runtime.run(_request())
    # The write would need approval, but a refused call is never put to the user.
    assert result.status == RunStatus.COMPLETED and result.output == "I could not."
    assert recorder.ran == []
    assert _results(provider) == [
        {"ok": False, "error": "host example.org is not allowed", "note": FIX_IT}
    ]
    events = await runtime.events.history(result.run_id)
    types = [event.type for event in events]
    assert "approval.required" not in types
    [refused] = [event for event in events if event.type == "tool.refused"]
    assert refused.data == {
        "tool": "config.change",
        "reason_code": "tool.precheck_refused",
        "refusal_count": 1,
        "refusal_limit_reached": False,
    }
    [completed] = [event for event in events if event.type == "tool.completed"]
    assert completed.data == {
        "tool": "config.change",
        "status": "refused",
        "reason_code": "tool.precheck_refused",
    }
    audits = await runtime.audits.list("t", "config.change")
    assert [(audit.action, audit.outcome) for audit in audits] == [("tool.call", "refused")]
    assert audits[0].metadata["reason_code"] == "tool.precheck_refused"
    state = await runtime.status(result.run_id, "t")
    assert state.tool_refusals == {"config.change": 1}


async def test_reads_are_not_prechecked() -> None:
    recorder = Recorder(refuse={"a": "never"})
    provider = MockModelProvider([_call("files.read", {"key": "a"}), "read"])
    runtime, _ = _runtime(
        provider, _tool("files.read", recorder.handler, precheck=recorder.precheck)
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    assert recorder.checked == [] and recorder.ran == [{"key": "a"}]


async def test_arguments_that_do_not_fit_the_schema_are_refused_before_approval() -> None:
    recorder = Recorder()
    provider = MockModelProvider([_call("config.change", {"key": 7}), "fixed nothing"])
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        tool_policy=RETURN,
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    # The schema is checked first; the precheck never sees arguments that cannot run.
    assert recorder.checked == [] and recorder.ran == []
    assert _results(provider) == [
        {
            "ok": False,
            "error": "Invalid arguments for config.change: key: 7 is not of type 'string'",
            "note": FIX_IT,
        }
    ]
    [refused] = [
        event
        for event in await runtime.events.history(result.run_id)
        if event.type == "tool.refused"
    ]
    assert refused.data["reason_code"] == "tool.schema_invalid"


async def test_bad_arguments_still_fail_a_fail_run_agent_but_nobody_is_asked() -> None:
    recorder = Recorder()
    runtime, _ = _runtime(
        MockModelProvider([_call("config.change", {"key": 7}), "never"]),
        _tool("config.change", recorder.handler, side_effect=SideEffect.WRITE),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.FAILED and recorder.ran == []
    assert "approval.required" not in await _event_types(runtime, result.run_id)


async def test_a_parallel_reply_is_prechecked_in_order_after_the_calls_that_will_run() -> None:
    recorder = Recorder(refuse={"b": "b depends on a host nobody allowed"})
    provider = MockModelProvider(
        [
            _calls(
                ("config.change", {"key": "a"}),
                ("config.change", {"key": "b"}),
                ("config.change", {"key": "c"}),
                ("files.read", {"key": "r"}),
            ),
            "two of three",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        _tool("files.read", recorder.handler),
        tool_policy=ToolPolicy(parallel_calls=True),
        approval_policy=NO_APPROVAL,
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    # Each call is judged after the earlier ones in the reply; a refused one changes nothing.
    assert recorder.checked == [
        ("config.change", {"key": "a"}, []),
        ("config.change", {"key": "b"}, [("config.change", {"key": "a"})]),
        ("config.change", {"key": "c"}, [("config.change", {"key": "a"})]),
    ]
    assert sorted(call["key"] for call in recorder.ran) == ["a", "c", "r"]
    results = _results(provider)
    assert [item.get("ok") for item in results] == [True, False, True, True]
    assert results[1]["error"] == "b depends on a host nobody allowed"
    assert result.execution_summary.tool_calls == 4


async def _mixed_reply_events(*, parallel: bool) -> list[tuple[str, str, str]]:
    """A reply whose first call (A) is refused and whose second (B) runs."""
    recorder = Recorder(refuse={"a": "a is not allowed"})
    first = next(_IDS)
    provider = MockModelProvider(
        [
            ModelResponse(
                message=Message.text(Role.ASSISTANT, ""),
                tool_calls=(
                    ToolCall(id=f"call-{first}-a", name="config.change", arguments={"key": "a"}),
                    ToolCall(id=f"call-{first}-b", name="config.change", arguments={"key": "b"}),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
                model="deterministic",
                provider="mock",
            ),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        tool_policy=ToolPolicy(on_error="return_to_model", parallel_calls=parallel),
        approval_policy=NO_APPROVAL,
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED and recorder.ran == [{"key": "b"}]
    events = await runtime.events.history(result.run_id)
    [held] = [
        event for event in events if event.type == "tool.started" and event.step_id.endswith("-a")
    ]
    assert held.data == {
        "tool": "config.change",
        "status": "refused",
        "side_effect": "write",
        "argument_keys": ["key"],
    }
    # ``(type, call, status)`` for each tool step's start, refusal and completion, in order.
    return [
        (event.type, event.step_id.rsplit("-", 1)[-1], event.data.get("status", ""))
        for event in events
        if event.step_id is not None
        and event.step_id.startswith("tool-")
        and event.type in {"step.started", "tool.started", "tool.refused", "tool.completed"}
    ]


async def test_a_refused_call_starts_like_one_that_runs_one_at_a_time() -> None:
    assert await _mixed_reply_events(parallel=False) == [
        ("step.started", "a", ""),
        ("tool.started", "a", "refused"),
        ("tool.refused", "a", ""),
        ("tool.completed", "a", "refused"),
        ("step.started", "b", ""),
        ("tool.started", "b", "running"),
        ("tool.completed", "b", "completed"),
    ]


async def test_a_parallel_reply_announces_its_calls_in_the_models_order() -> None:
    # A running call's start never comes before an earlier call a guard held back, and
    # every call finishes in the order the model sent them.
    assert await _mixed_reply_events(parallel=True) == [
        ("step.started", "a", ""),
        ("step.started", "b", ""),
        ("tool.started", "a", "refused"),
        ("tool.started", "b", "running"),
        ("tool.refused", "a", ""),
        ("tool.completed", "a", "refused"),
        ("tool.completed", "b", "completed"),
    ]


async def test_a_held_back_read_starts_with_its_guard_status() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [_call("files.read", {"key": "a"}), _call("files.read", {"key": "a"}), "done"]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        tool_policy=ToolPolicy(repeat_guard=True),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    events = await runtime.events.history(result.run_id)
    started = [event.data["status"] for event in events if event.type == "tool.started"]
    completed = [
        (event.data["status"], event.data.get("reason_code"))
        for event in events
        if event.type == "tool.completed"
    ]
    assert started == ["running", "repeated"]
    assert completed == [("completed", None), ("repeated", "tool.repeated")]


async def test_a_refused_call_is_recorded_as_a_tool_span() -> None:
    exporter = InMemorySpanExporter()
    provider_ = TracerProvider()
    provider_.add_span_processor(SimpleSpanProcessor(exporter))
    recorder = Recorder(refuse={"bad": "no"})
    runtime, _ = _runtime(
        MockModelProvider([_call("config.change", {"key": "bad"}), "ok"]),
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
    )
    runtime._tracer = provider_.get_tracer("test")  # type: ignore[assignment]
    await runtime.run(_request())
    [span] = [
        span
        for span in exporter.get_finished_spans()
        if span.name == "agent.tool.call" and span.attributes
    ]
    attributes = dict(span.attributes or {})
    assert attributes["tool.status"] == "refused"
    assert attributes["tool.guard.reason_code"] == "tool.precheck_refused"
    assert attributes["tool.guard.refusal_count"] == 1


# 2. Refusal limits


async def test_from_the_limit_the_refusal_says_stop_and_tell_the_user() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    bad = ("config.change", {"key": "bad"})
    provider = MockModelProvider([_call(*bad), _call(*bad), _call(*bad), "I gave up."])
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        tool_policy=ToolPolicy(max_refusals_per_tool=2),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED and recorder.ran == []
    notes = [item["note"] for item in _results(provider)]
    assert notes == [FIX_IT, STOP.format(count=2), STOP.format(count=3)]
    assert "do not call it again" in notes[1]
    refused = [
        event.data
        for event in await runtime.events.history(result.run_id)
        if event.type == "tool.refused"
    ]
    assert [data["refusal_limit_reached"] for data in refused] == [False, True, True]
    assert (await runtime.status(result.run_id, "t")).tool_refusals == {"config.change": 3}


async def test_without_a_limit_a_refusal_always_says_fix_it() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    bad = ("config.change", {"key": "bad"})
    provider = MockModelProvider([_call(*bad), _call(*bad), _call(*bad), "done"])
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
    )
    await runtime.run(_request())
    assert [item["note"] for item in _results(provider)] == [FIX_IT] * 3


async def test_schema_failures_count_toward_the_limit_per_tool() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    provider = MockModelProvider(
        [
            _call("config.change", {"key": 1}),
            _call("files.read", {"key": 2}),
            _call("config.change", {"key": "bad"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        _tool("files.read", recorder.handler),
        tool_policy=ToolPolicy(on_error="return_to_model", max_refusals_per_tool=2),
    )
    result = await runtime.run(_request())
    assert [item["note"] for item in _results(provider)] == [
        FIX_IT,
        FIX_IT,
        STOP.format(count=2),
    ]
    state = await runtime.status(result.run_id, "t")
    assert state.tool_refusals == {"config.change": 2, "files.read": 1}


async def test_refusals_counted_before_an_approval_pause_count_after_it() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    provider = MockModelProvider(
        [
            _call("config.change", {"key": "bad"}),
            _call("config.change", {"key": "good"}),
            _call("config.change", {"key": "bad"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        tool_policy=ToolPolicy(max_refusals_per_tool=2, refusal_count="per_run"),
    )
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert (await runtime.status(paused.run_id, "t")).tool_refusals == {"config.change": 1}
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    state = await _settle(runtime, paused.run_id)
    assert state.status == RunStatus.COMPLETED
    assert recorder.ran == [{"key": "good"}]
    assert _results(provider)[-1]["note"] == STOP.format(count=2)


async def test_a_successful_call_starts_the_tools_refusal_count_again() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    bad, good = ("config.change", {"key": "bad"}), ("config.change", {"key": "good"})
    provider = MockModelProvider(
        [_call(*bad), _call(*good), _call(*bad), _call(*bad), _call(*bad), "done"]
    )
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        tool_policy=ToolPolicy(max_refusals_per_tool=3),
        approval_policy=NO_APPROVAL,
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED and recorder.ran == [{"key": "good"}]
    # Refused, succeeded, refused, refused: two in a row, under the limit of three.
    notes = [item.get("note") for item in _results(provider)]
    assert notes == [FIX_IT, None, FIX_IT, FIX_IT, STOP.format(count=3)]
    refused = [
        event.data["refusal_count"]
        for event in await runtime.events.history(result.run_id)
        if event.type == "tool.refused"
    ]
    assert refused == [1, 1, 2, 3]
    assert (await runtime.status(result.run_id, "t")).tool_refusals == {"config.change": 3}


async def test_per_run_counts_every_refusal_in_the_run() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    bad, good = ("config.change", {"key": "bad"}), ("config.change", {"key": "good"})
    provider = MockModelProvider([_call(*bad), _call(*good), _call(*bad), _call(*bad), "done"])
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        tool_policy=ToolPolicy(max_refusals_per_tool=3, refusal_count="per_run"),
        approval_policy=NO_APPROVAL,
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    assert recorder.ran == [{"key": "good"}]
    notes = [item.get("note") for item in _results(provider)]
    assert notes == [FIX_IT, None, FIX_IT, STOP.format(count=3)]


async def test_another_tools_success_or_a_failed_call_does_not_reset_the_count() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})

    def handler(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        if arguments["key"] == "boom":
            raise RuntimeError("boom")
        return recorder.handler(arguments, context)

    provider = MockModelProvider(
        [
            _call("config.change", {"key": "bad"}),
            _call("files.read", {"key": "r"}),
            _call("config.change", {"key": "boom"}),
            _call("config.change", {"key": "bad"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool("config.change", handler, side_effect=SideEffect.WRITE, precheck=recorder.precheck),
        _tool("files.read", recorder.handler),
        tool_policy=ToolPolicy(on_error="return_to_model", max_refusals_per_tool=2),
        approval_policy=NO_APPROVAL,
    )
    await runtime.run(_request())
    results = _results(provider)
    assert "error" in results[2] and results[-1]["note"] == STOP.format(count=2)


async def test_a_consecutive_count_survives_an_approval_pause() -> None:
    recorder = Recorder(refuse={"bad": "invalid"})
    provider = MockModelProvider(
        [
            _call("config.change", {"key": "bad"}),
            _call("files.write", {"key": "w"}),
            _call("config.change", {"key": "bad"}),
            _call("config.change", {"key": "good"}),
            _call("config.change", {"key": "bad"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool(
            "config.change",
            recorder.handler,
            side_effect=SideEffect.WRITE,
            precheck=recorder.precheck,
        ),
        _tool("files.write", recorder.handler, side_effect=SideEffect.WRITE),
        tool_policy=ToolPolicy(max_refusals_per_tool=2),
        approval_policy=ApprovalPolicy(require_for_side_effects=True),
    )
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    assert (await runtime.status(paused.run_id, "t")).tool_refusals == {"config.change": 1}
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    state = await _settle(runtime, paused.run_id)
    # Another tool's success does not reset config.change: the count after the pause is two.
    assert state.status == RunStatus.AWAITING_APPROVAL
    assert _results(provider)[-1]["note"] == STOP.format(count=2)
    # The approved good change resets it, and the reset is checkpointed.
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    state = await _settle(runtime, paused.run_id)
    assert state.status == RunStatus.COMPLETED
    assert _results(provider)[-1]["note"] == FIX_IT
    assert state.tool_refusals == {"config.change": 1}


# 3. Repeat guard and per-tool call limits


def _reads(*keys: str) -> list[ModelResponse | str]:
    return [*(_call("files.read", {"key": key}) for key in keys), "done"]


async def test_a_repeated_read_returns_a_note_instead_of_running() -> None:
    recorder = Recorder()
    provider = MockModelProvider(_reads("a", "b", "a"))
    runtime, _ = _runtime(
        provider, _tool("files.read", recorder.handler), tool_policy=ToolPolicy(repeat_guard=True)
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    assert recorder.ran == [{"key": "a"}, {"key": "b"}]
    assert _results(provider)[-1] == {"ok": False, "repeated": True, "error": REPEATED}
    events = await runtime.events.history(result.run_id)
    [repeated] = [event for event in events if event.type == "tool.repeated"]
    assert repeated.data == {"tool": "files.read", "reason_code": "tool.repeated"}
    assert [event.data["status"] for event in events if event.type == "tool.completed"] == [
        "completed",
        "completed",
        "repeated",
    ]
    audits = await runtime.audits.list("t", "files.read")
    assert [audit.outcome for audit in audits] == ["completed", "completed", "repeated"]


async def test_repeats_run_by_default() -> None:
    recorder = Recorder()
    runtime, _ = _runtime(
        MockModelProvider(_reads("a", "a")), _tool("files.read", recorder.handler)
    )
    await runtime.run(_request())
    assert recorder.ran == [{"key": "a"}, {"key": "a"}]


async def test_a_successful_change_lets_reads_run_again() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [
            _call("files.read", {"key": "a"}),
            _call("files.write", {"key": "a"}),
            _call("files.read", {"key": "a"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        _tool("files.write", recorder.handler, side_effect=SideEffect.WRITE),
        tool_policy=ToolPolicy(repeat_guard=True),
        approval_policy=NO_APPROVAL,
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    assert len(recorder.ran) == 3


async def test_a_failed_read_is_forgotten_so_its_retry_runs() -> None:
    attempts: list[int] = []

    def flaky(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("busy")
        return {"ok": True}

    provider = MockModelProvider(_reads("a", "a"))
    read = _tool("files.read", flaky)
    # One attempt, so the failure reaches the model rather than the executor's retry.
    read.definition = read.definition.model_copy(
        update={"retry_policy": RetryPolicy(max_attempts=1)}
    )
    runtime, _ = _runtime(
        provider,
        read,
        tool_policy=ToolPolicy(on_error="return_to_model", repeat_guard=True),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED and len(attempts) == 2
    assert _results(provider)[-1] == {"ok": True}


async def test_a_volatile_read_is_never_held_back() -> None:
    recorder = Recorder()
    runtime, _ = _runtime(
        MockModelProvider(_reads("a", "a")),
        _tool("files.read", recorder.handler, volatile=True),
        tool_policy=ToolPolicy(repeat_guard=True),
    )
    await runtime.run(_request())
    assert len(recorder.ran) == 2


async def test_a_tool_past_its_call_limit_returns_the_over_limit_note() -> None:
    recorder = Recorder()
    provider = MockModelProvider(_reads("a", "b", "c"))
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        tool_policy=ToolPolicy(max_calls_per_tool={"files.read": 2}),
    )
    result = await runtime.run(_request())
    assert recorder.ran == [{"key": "a"}, {"key": "b"}]
    assert _results(provider)[-1] == {
        "ok": False,
        "repeated": True,
        "error": over_limit("files.read", 2),
    }
    [held] = [
        event.data
        for event in await runtime.events.history(result.run_id)
        if event.type == "tool.repeated"
    ]
    assert held == {"tool": "files.read", "reason_code": "tool.call_limit", "call_limit": 2}
    assert (await runtime.status(result.run_id, "t")).tool_call_counts == {"files.read": 2}


async def test_a_parallel_reply_holds_back_duplicates_and_calls_past_the_limit() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [
            _calls(
                ("files.read", {"key": "a"}),
                ("files.read", {"key": "a"}),
                ("files.read", {"key": "b"}),
                ("files.search", {"key": "x"}),
                ("files.search", {"key": "y"}),
            ),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        _tool("files.search", recorder.handler),
        tool_policy=ToolPolicy(
            parallel_calls=True,
            max_parallel_calls=8,
            repeat_guard=True,
            max_calls_per_tool={"files.search": 1},
        ),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    assert sorted(call["key"] for call in recorder.ran) == ["a", "b", "x"]
    results = _results(provider)
    assert results[1] == {"ok": False, "repeated": True, "error": REPEATED}
    assert results[4]["error"] == over_limit("files.search", 1)
    assert [item.get("ok") for item in results] == [True, False, True, True, False]


async def test_seen_reads_and_call_counts_survive_an_approval_pause() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [
            _call("files.read", {"key": "a"}),
            _call("files.write", {"key": "w"}),
            _call("files.read", {"key": "a"}),
            _call("files.read", {"key": "b"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        _tool("files.write", recorder.handler, side_effect=SideEffect.WRITE),
        tool_policy=ToolPolicy(repeat_guard=True, max_calls_per_tool={"files.read": 1}),
        approval_policy=ApprovalPolicy(on_rejection="continue"),
    )
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    saved = await runtime.status(paused.run_id, "t")
    assert len(saved.seen_read_calls) == 1 and saved.tool_call_counts == {"files.read": 1}
    # A declined change changed nothing: the earlier read is still a repeat.
    await runtime.resume(paused.run_id, "t", {"decision": "rejected"})
    state = await _settle(runtime, paused.run_id)
    assert state.status == RunStatus.COMPLETED
    assert recorder.ran == [{"key": "a"}]
    results = _results(provider)
    # The call limit is checked first: both later reads are past it.
    assert results[2]["error"] == over_limit("files.read", 1)
    assert results[3]["error"] == over_limit("files.read", 1)


async def test_a_repeat_after_an_approved_change_runs() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [
            _call("files.read", {"key": "a"}),
            _call("files.write", {"key": "w"}),
            _call("files.read", {"key": "a"}),
            _call("files.read", {"key": "a"}),
            "done",
        ]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        _tool("files.write", recorder.handler, side_effect=SideEffect.WRITE),
        tool_policy=ToolPolicy(repeat_guard=True),
    )
    paused = await runtime.run(_request())
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    state = await _settle(runtime, paused.run_id)
    assert state.status == RunStatus.COMPLETED
    assert recorder.ran == [{"key": "a"}, {"key": "w"}, {"key": "a"}]
    assert _results(provider)[-1]["error"] == REPEATED


# 4. Room to answer


def test_tool_calls_left_leaves_two_steps_for_the_answer() -> None:
    # Each tool call costs its own step and the model call that reads it.
    assert [tool_calls_left(10, used) for used in range(0, 11, 2)] == [4, 3, 2, 1, 0, 0]
    assert tool_calls_left(10, 7) == 0


async def test_near_the_step_limit_tool_results_carry_a_wrap_up_notice() -> None:
    recorder = Recorder()
    provider = MockModelProvider(_reads("a", "b", "c"))
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        max_steps=12,
        wrap_up_policy=WrapUpPolicy(enabled=True),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    messages = _tool_messages(provider)
    # Steps used after each call: 2, 4, 6 of 12 -> 4, 3, 2 calls left; the notice comes at 2.
    assert [len(message.content) for message in messages] == [1, 1, 2]
    assert messages[2].content[1].text == wrap_up_note(2)  # type: ignore[union-attr]
    # The tool's own result is untouched.
    assert json.loads(messages[2].content[0].text) == {"ok": True, "key": "c"}  # type: ignore[union-attr]
    events = await runtime.events.history(result.run_id)
    [notice] = [event for event in events if event.type == "tool.wrap_up"]
    assert notice.data == {"tool_calls_left": 2, "reason": "steps"}
    audits = [audit for audit in await runtime.audits.list("t") if audit.action == "run.wrap_up"]
    assert [audit.outcome for audit in audits] == ["notified"]


async def test_no_notice_by_default() -> None:
    provider = MockModelProvider(_reads("a", "b", "c"))
    runtime, _ = _runtime(provider, _tool("files.read", Recorder().handler), max_steps=8)
    await runtime.run(_request())
    assert all(len(message.content) == 1 for message in _tool_messages(provider))


async def test_the_token_budget_brings_the_notice_forward_and_then_says_answer_now() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [
            _call("files.read", {"key": "a"}, tokens=500),
            _call("files.read", {"key": "b"}, tokens=300),
            _call("files.read", {"key": "c"}, tokens=150),
            "done",
        ]
    )
    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        max_steps=100,
        budget=Budget(max_tokens=1000),
        wrap_up_policy=WrapUpPolicy(enabled=True),
    )
    runtime._tracer = tracing.get_tracer("test")  # type: ignore[assignment]
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    messages = _tool_messages(provider)
    # 50% of the budget: no notice; 80%: wrap up with two calls left; 95%: answer now.
    assert [len(message.content) for message in messages] == [1, 2, 2]
    assert messages[1].content[1].text == wrap_up_note(2)  # type: ignore[union-attr]
    assert messages[2].content[1].text == wrap_up_note(0)  # type: ignore[union-attr]
    notices = [
        event.data
        for event in await runtime.events.history(result.run_id)
        if event.type == "tool.wrap_up"
    ]
    assert notices == [
        {"tool_calls_left": 2, "reason": "tokens"},
        {"tool_calls_left": 0, "reason": "tokens"},
    ]
    marked = [
        span.attributes["agent.wrap_up.tool_calls_left"]
        for span in exporter.get_finished_spans()
        if span.attributes and "agent.wrap_up.tool_calls_left" in span.attributes
    ]
    assert marked == [2, 0]


async def test_no_notice_while_automatic_extensions_are_left() -> None:
    provider = MockModelProvider(_reads("a", "b", "c"))
    runtime, _ = _runtime(
        provider,
        _tool("files.read", Recorder().handler),
        max_steps=4,
        wrap_up_policy=WrapUpPolicy(enabled=True),
        continuation_policy=ContinuationPolicy(
            enabled=True, automatic_extensions=1, step_increment=6
        ),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    # Steps 2 and 4 of 4 come before the extension, which is still to come, so no notice.
    # After it the limit (10) is final: 6 used -> 1 call left.
    assert [len(message.content) for message in _tool_messages(provider)] == [1, 1, 2]


async def test_every_result_of_a_parallel_reply_carries_the_notice() -> None:
    provider = MockModelProvider(
        [_calls(("files.read", {"key": "a"}), ("files.read", {"key": "b"})), "done"]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", Recorder().handler),
        max_steps=8,
        tool_policy=ToolPolicy(parallel_calls=True),
        wrap_up_policy=WrapUpPolicy(enabled=True),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    # Model call and two calls: three steps of eight -> (8 - 3 - 2) // 2 = 1 call left.
    assert [message.content[1].text for message in _tool_messages(provider)] == [  # type: ignore[union-attr]
        wrap_up_note(1),
        wrap_up_note(1),
    ]


async def test_a_held_back_call_carries_the_notice_too() -> None:
    provider = MockModelProvider(_reads("a", "a"))
    runtime, _ = _runtime(
        provider,
        _tool("files.read", Recorder().handler),
        max_steps=8,
        tool_policy=ToolPolicy(repeat_guard=True),
        wrap_up_policy=WrapUpPolicy(enabled=True, tool_calls_left=1),
    )
    await runtime.run(_request())
    second = _tool_messages(provider)[1]
    assert json.loads(second.content[0].text)["error"] == REPEATED  # type: ignore[union-attr]
    # 4 of 8 steps used -> 1 call left, the policy's threshold.
    assert second.content[1].text == wrap_up_note(1)  # type: ignore[union-attr]


async def test_the_notice_counts_steps_used_before_an_approval_pause() -> None:
    recorder = Recorder()
    provider = MockModelProvider(
        [_call("files.read", {"key": "a"}), _call("files.write", {"key": "w"}), "done"]
    )
    runtime, _ = _runtime(
        provider,
        _tool("files.read", recorder.handler),
        _tool("files.write", recorder.handler, side_effect=SideEffect.WRITE),
        max_steps=11,
        wrap_up_policy=WrapUpPolicy(enabled=True),
    )
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_APPROVAL
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    state = await _settle(runtime, paused.run_id)
    assert state.status == RunStatus.COMPLETED
    first, second = _tool_messages(provider)
    assert len(first.content) == 1
    # The paused step and its resumed attempt both count: 5 of 11 used -> 2 calls left.
    assert second.content[1].text == wrap_up_note(2)  # type: ignore[union-attr]


# Checkpoints and definitions from before these fields


async def test_typed_tools_take_a_precheck_and_volatile() -> None:
    async def no_deletes(
        definition: ToolDefinition, arguments: Mapping[str, Any], earlier: Sequence[EarlierCall]
    ) -> str | None:
        return "cannot delete the main branch" if arguments["branch"] == "main" else None

    @typed_tool(write(), precheck=no_deletes, volatile=False)
    def delete_branch(branch: str) -> str:
        """Delete a branch."""
        return branch

    @typed_tool(volatile=True)
    def build_status() -> str:
        """The current build's status."""
        return "green"

    assert await delete_branch.check({"branch": "main"}) == "cannot delete the main branch"
    assert await delete_branch.check({"branch": "topic"}) is None
    assert build_status.definition.volatile and not delete_branch.definition.volatile
    # The live clock changes on its own; a fixed one does not.
    assert clock_tool().definition.volatile
    assert not clock_tool(lambda: datetime(2026, 1, 1, tzinfo=UTC)).definition.volatile


def test_a_checkpoint_saved_without_the_guard_fields_still_loads() -> None:
    state = RunState(request=_request())
    saved = json.loads(state.model_dump_json())
    for field in ("tool_refusals", "tool_call_counts", "seen_read_calls"):
        saved.pop(field)
    restored = RunState.model_validate(saved)
    assert restored.tool_refusals == {} and restored.tool_call_counts == {}
    assert restored.seen_read_calls == set()
    # And the new state round-trips through a checkpoint.
    restored.tool_refusals["x"] = 2
    restored.seen_read_calls.add("k")
    again = RunState.model_validate_json(restored.model_dump_json())
    assert again.tool_refusals == {"x": 2} and again.seen_read_calls == {"k"}


def test_an_agent_saved_without_the_new_policies_still_loads() -> None:
    saved = json.loads(make_agent().model_dump_json())
    saved.pop("wrap_up_policy")
    for field in ("max_refusals_per_tool", "repeat_guard", "max_calls_per_tool"):
        saved["tool_policy"].pop(field)
    agent = AgentDefinition.model_validate(saved)
    assert agent.wrap_up_policy == WrapUpPolicy()
    assert agent.tool_policy == ToolPolicy()


# agent.yaml

GUARDED_YAML = """
providers:
  local: {type: mock, default_model: m}
security:
  allowed_http_hosts: [jobs.example.com]
http_tools:
  - name: jobs.status
    description: A job's current status.
    url: https://jobs.example.com/status
    volatile: true
agents:
  - name: operator
    version: 1.0.0
    description: d
    system_instructions: x
    default_model: {name: m, provider: local}
    enabled_tools: [jobs.status]
    tool_policy:
      on_error: return_to_model
      max_refusals_per_tool: 3
      repeat_guard: true
      max_calls_per_tool: {jobs.status: 5}
    wrap_up_policy:
      enabled: true
      tool_calls_left: 2
      token_wrap_up_share: 0.75
      token_answer_share: 0.9
"""


def test_agent_yaml_spells_the_guards_and_check_accepts_them(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(GUARDED_YAML)
    report = check_project((config,))
    assert report.ok, [issue.message for issue in report.errors]
    settings = load_settings((config,))
    [agent] = settings.agents
    assert agent.tool_policy.max_calls_per_tool == {"jobs.status": 5}
    assert agent.tool_policy.repeat_guard and agent.wrap_up_policy.enabled
    described = {tool.name: tool for tool in describe_components(settings).tools}
    assert described["jobs.status"].volatile is True
    assert described["core.http"].volatile is False


def test_a_misspelled_guard_setting_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text(GUARDED_YAML.replace("tool_calls_left: 2", "tool_calls_lef: 2"))
    report = check_project((config,))
    assert [issue.code for issue in report.errors] == ["CONFIGURATION_ERROR"]
