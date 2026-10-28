"""Per-run tool credentials: memory only, read-only, never persisted or reported."""

from __future__ import annotations

import asyncio
import copy
import logging
import pickle
from datetime import timedelta
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.observability.traccia_adapter import NoopObservabilityAdapter
from algen_agent_runtime.observability.trace_levels import TraceLevelTracer
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.subagent import subagent_tool
from algen_agent_runtime.tools.testing import fake_tool_context
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunRequest,
    RunState,
    RunStatus,
    ToolCall,
    ToolPolicy,
    utc_now,
)

SECRET = "cred-7f3a9c41-do-not-leak"
NEWER = "cred-b81e02d5-after-resume"

OBJECT = {"type": "object", "properties": {}, "additionalProperties": False}


def request(**updates: Any) -> RunRequest:
    values: dict[str, Any] = {
        "agent": "test-agent",
        "input": "go",
        "tenant_id": "t",
        "user_id": "u",
    }
    values.update(updates)
    return RunRequest(**values)


class Seen:
    """What each tool call saw in ``context.credentials``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.contexts: list[ToolContext] = []

    def tool(
        self,
        name: str = "api.read",
        *,
        side_effect: SideEffect = SideEffect.READ,
        delay: float = 0,
    ) -> Tool:
        async def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
            if delay:
                await asyncio.sleep(delay)
            self.calls.append((context.run_id, dict(context.credentials)))
            self.contexts.append(context)
            return {"ok": True}

        return Tool(
            ToolDefinition(
                name=name,
                version="1",
                description="Read the caller's records.",
                input_schema=OBJECT,
                output_schema={"type": "object"},
                side_effect=side_effect,
                idempotency="keyed",
            ),
            handler,
        )

    @property
    def credentials(self) -> list[dict[str, str]]:
        return [seen for _, seen in self.calls]


def runtime_with(
    seen: Seen, *responses: str | ModelResponse, delay: float = 0, **agent: Any
) -> tuple[AgentRuntime, MockModelProvider]:
    provider = MockModelProvider(list(responses))
    tools = {"api.read", "api.write", *agent.pop("tools", ())}
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset(tools),
            tool_permissions=frozenset(agent.pop("permissions", ())),
            **agent,
        ),
    )
    runtime.tools.register(seen.tool("api.read", delay=delay))
    runtime.tools.register(seen.tool("api.write", side_effect=SideEffect.WRITE))
    return runtime, provider


def parallel_reply(name: str, count: int) -> ModelResponse:
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=tuple(ToolCall(id=f"call-{i}", name=name, arguments={}) for i in range(count)),
        finish_reason=FinishReason.TOOL_CALLS,
        model="deterministic",
        provider="mock",
    )


async def paused_for_approval(runtime: AgentRuntime) -> str:
    result = await runtime.run(request(), credentials={"authorization": SECRET})
    assert result.status == RunStatus.AWAITING_APPROVAL, result.error
    return result.run_id


# --- ToolContext ---------------------------------------------------------------------------------


def test_tool_context_hides_credentials_from_repr_dump_equality_and_hash() -> None:
    context = fake_tool_context(credentials={"authorization": SECRET})
    other = context.model_copy(update={"credentials": {"authorization": "different"}})

    assert context.credentials["authorization"] == SECRET
    for text in (
        repr(context),
        str(context),
        repr(context.credentials),
        str(context.model_dump()),
        context.model_dump_json(),
        str(context.model_dump(mode="json")),
    ):
        assert SECRET not in text
    assert context == other and hash(context) == hash(other)
    assert "credentials" not in context.model_dump()
    # A pickled copy (another process, a disk) carries none; a deep copy keeps them in memory.
    assert dict(pickle.loads(pickle.dumps(context)).credentials) == {}
    assert copy.deepcopy(context).credentials["authorization"] == SECRET


def test_tool_context_credentials_are_a_read_only_private_copy() -> None:
    supplied = {"authorization": SECRET}
    context = fake_tool_context(credentials=supplied)
    supplied["authorization"] = "changed"

    assert context.credentials["authorization"] == SECRET
    with pytest.raises(TypeError):
        context.credentials["authorization"] = "x"  # type: ignore[index]
    assert dict(fake_tool_context().credentials) == {}


def test_invalid_credentials_are_rejected_without_echoing_them() -> None:
    with pytest.raises(ValueError) as caught:
        ToolContext(
            run_id="r",
            step_id="s",
            tenant_id="t",
            user_id="u",
            permissions=frozenset(),
            idempotency_key="k",
            credentials={"authorization": SECRET, "count": 1},  # type: ignore[dict-item]
        )
    assert SECRET not in str(caught.value)


async def test_runtime_rejects_invalid_credentials_before_creating_a_run() -> None:
    seen = Seen()
    runtime, _ = runtime_with(seen, "answer")
    with pytest.raises(TypeError) as caught:
        await runtime.start(request(), credentials={"authorization": SECRET, "n": 1})  # type: ignore[dict-item]
    assert SECRET not in str(caught.value)
    assert await runtime.runs.list_active(10) == ()


# --- Spec tests ----------------------------------------------------------------------------------


async def test_tool_sees_the_credentials_passed_to_run() -> None:
    seen = Seen()
    runtime, _ = runtime_with(seen, tool_call_response("api.read", {}), "done")
    supplied = {"authorization": SECRET}

    state = await runtime.start(request(), credentials=supplied)
    supplied["authorization"] = "changed by the caller after start"
    result = await runtime.wait(state.id, "t")

    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{"authorization": SECRET}]
    with pytest.raises(TypeError):
        seen.contexts[0].credentials["authorization"] = "x"  # type: ignore[index]


async def test_resume_shows_only_the_new_credentials() -> None:
    seen = Seen()
    runtime, _ = runtime_with(
        seen, tool_call_response("api.write", {}), "done", permissions=("write",)
    )
    run_id = await paused_for_approval(runtime)

    await runtime.resume(
        run_id, "t", {"decision": "approved"}, credentials={"authorization": NEWER}
    )
    result = await runtime.wait(run_id, "t")

    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{"authorization": NEWER}]


async def test_resume_without_credentials_gives_the_tool_none() -> None:
    seen = Seen()
    runtime, _ = runtime_with(seen, tool_call_response("api.write", {}), "done")
    run_id = await paused_for_approval(runtime)

    await runtime.resume(run_id, "t", {"decision": "approved"})
    result = await runtime.wait(run_id, "t")

    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{}]


async def test_clarification_resume_takes_new_credentials() -> None:
    seen = Seen()
    runtime, _ = runtime_with(seen, tool_call_response("api.read", {}), "done")
    state = RunState(
        request=request(),
        agent_key="test-agent@1.0.0",
        status=RunStatus.AWAITING_CLARIFICATION,
        pause_payload={"question": "Which account?"},
        deadline=utc_now() + timedelta(seconds=30),
    )
    await runtime.runs.create(state)

    await runtime.resume(
        state.id, "t", {"clarification": "the main one"}, credentials={"authorization": NEWER}
    )
    result = await runtime.wait(state.id, "t")

    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{"authorization": NEWER}]
    assert runtime._credentials == {}


async def test_recovered_run_gives_the_tool_no_credentials() -> None:
    seen = Seen()
    blocked = asyncio.Event()

    class BlockingProvider(MockModelProvider):
        async def generate(self, model_request: Any) -> ModelResponse:
            blocked.set()
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    first, _ = runtime_with(seen)
    first.router._providers["mock"] = BlockingProvider([])
    state = await first.start(request(), credentials={"authorization": SECRET})
    await asyncio.wait_for(blocked.wait(), 5)
    assert first._credentials == {state.id: {"authorization": SECRET}}
    await first.shutdown(grace_seconds=0)
    assert first._credentials == {}

    # A new process: same durable run store, nothing in memory.
    second, _ = runtime_with(seen, tool_call_response("api.read", {}), "done")
    second.runs = first.runs
    assert await second.recover() == 1
    result = await second.wait(state.id, "t")

    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{}]


async def test_credentials_are_dropped_from_memory_when_the_run_pauses_or_ends() -> None:
    seen = Seen()
    runtime, _ = runtime_with(
        seen,
        tool_call_response("api.check", {}),
        tool_call_response("api.write", {}),
        "done",
        permissions=("write",),
        tools=("api.check",),
    )
    held: list[bool] = []

    async def check(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        held.append(context.run_id in runtime._credentials)
        return {"ok": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="api.check",
                version="1",
                description="Read.",
                input_schema=OBJECT,
                output_schema={"type": "object"},
            ),
            check,
        )
    )

    run_id = await paused_for_approval(runtime)
    assert held == [True]  # held while the run executed
    assert runtime._credentials == {}  # dropped at the pause

    await runtime.resume(
        run_id, "t", {"decision": "approved"}, credentials={"authorization": NEWER}
    )
    result = await runtime.wait(run_id, "t")
    assert result.status == RunStatus.COMPLETED, result.error
    assert runtime._credentials == {}  # dropped at the end
    assert runtime._tasks == {}


async def test_credentials_are_dropped_when_a_run_fails_or_is_cancelled() -> None:
    async def explode(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        raise RuntimeError("boom")

    runtime, _ = runtime_with(Seen(), tool_call_response("api.fail", {}), tools=("api.fail",))
    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="api.fail",
                version="1",
                description="Fail.",
                input_schema=OBJECT,
                output_schema={"type": "object"},
                retry_policy={"max_attempts": 1},
            ),
            explode,
        )
    )
    failed = await runtime.run(request(), credentials={"authorization": SECRET})
    assert failed.status == RunStatus.FAILED
    assert runtime._credentials == {}

    started = asyncio.Event()

    async def slow(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(60)
        return {"ok": True}

    seen = Seen()
    slow_runtime, _ = runtime_with(seen, tool_call_response("api.slow", {}), tools=("api.slow",))
    slow_runtime.tools.register(
        Tool(
            ToolDefinition(
                name="api.slow",
                version="1",
                description="Slow.",
                input_schema=OBJECT,
                output_schema={"type": "object"},
                timeout_seconds=120,
            ),
            slow,
        )
    )
    state = await slow_runtime.start(request(), credentials={"authorization": SECRET})
    await asyncio.wait_for(started.wait(), 5)
    assert state.id in slow_runtime._credentials
    cancelled = await slow_runtime.cancel(state.id, "t")
    assert cancelled.status == RunStatus.CANCELLED
    assert slow_runtime._credentials == {}
    await asyncio.sleep(0)
    assert slow_runtime._credentials == {}


async def test_credentials_never_reach_state_events_audits_spans_logs_or_caches(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))

    class Recording(NoopObservabilityAdapter):
        def tracer(self, name: str) -> Any:
            return tracer_provider.get_tracer(name)

    seen = Seen()
    runtime, provider = runtime_with(
        seen,
        tool_call_response("api.leaky", {}),
        tool_call_response("api.crash", {}),
        tool_call_response("api.read", {}),
        tool_call_response("api.write", {}),
        "done",
        permissions=("write",),
        tools=("api.leaky", "api.crash"),
        tool_policy=ToolPolicy(on_error="return_to_model"),
        max_steps=20,
    )
    runtime.observability = Recording("detailed")
    runtime._tracer = TraceLevelTracer(tracer_provider.get_tracer("runtime"), "detailed")
    runtime.tool_executor._tracer = tracer_provider.get_tracer("tools")
    runtime.telemetry_include_content = True
    cache = CacheService(
        InMemoryCacheStore(),
        {
            "model_responses": CachePolicy(enabled=True, scope=CacheScope.USER),
            "tool_results": CachePolicy(enabled=True, scope=CacheScope.USER),
        },
    )
    runtime.cache = cache
    runtime.tool_executor._cache = cache

    async def leaky(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # A runtime error message reaches the model and events as-is: it may only carry the
        # context's repr, which shows no credentials.
        raise ToolExecutionError(
            f"leaky failed with {context!r} / {context} / {context.model_dump()}"
        )

    async def crash(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        raise RuntimeError(f"crash with {context!r} {context.credentials!r}")

    for name, handler in (("api.leaky", leaky), ("api.crash", crash)):
        runtime.tools.register(
            Tool(
                ToolDefinition(
                    name=name,
                    version="1",
                    description="Fails.",
                    input_schema=OBJECT,
                    output_schema={"type": "object"},
                    retry_policy={"max_attempts": 1},
                ),
                handler,
            )
        )

    paused = await runtime.run(request(), credentials={"authorization": SECRET})
    assert paused.status == RunStatus.AWAITING_APPROVAL, paused.error
    approval_id = str((await runtime.status(paused.run_id, "t")).pause_payload["approval_id"])  # type: ignore[index]
    approval = await runtime.approvals.get(approval_id, "t")
    assert approval is not None
    paused_state = (await runtime.status(paused.run_id, "t")).model_dump_json()
    await runtime.resume(
        paused.run_id, "t", {"decision": "approved"}, credentials={"authorization": NEWER}
    )
    result = await runtime.wait(paused.run_id, "t")
    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{"authorization": SECRET}, {"authorization": NEWER}]

    # A run that fails on a tool error is logged with its traceback.
    strict = make_agent(name="strict-agent", enabled_tools=frozenset({"api.crash"}))
    runtime.agents.register(strict)
    provider.queue(tool_call_response("api.crash", {}))
    failed = await runtime.run(request(agent="strict-agent"), credentials={"authorization": SECRET})
    assert failed.status == RunStatus.FAILED

    surfaces: list[str] = [paused_state, approval.model_dump_json(), result.model_dump_json()]
    for run_id in (paused.run_id, failed.run_id):
        state = await runtime.status(run_id, "t")
        surfaces.append(state.model_dump_json())
        surfaces.extend(event.model_dump_json() for event in await runtime.events.history(run_id))
        surfaces.append((await runtime.narrative(run_id, "t")).model_dump_json())
    surfaces.extend(audit.model_dump_json() for audit in await runtime.audits.list("t"))
    surfaces.extend(model_request.model_dump_json() for model_request in provider.requests)
    for span in exporter.get_finished_spans():
        surfaces.append(span.to_json())
    surfaces.extend(repr(value) for value in cache.store._items.values())  # type: ignore[attr-defined]
    surfaces.extend(repr(record) for record in runtime.tool_executor._executions._records.values())  # type: ignore[attr-defined]
    captured = capsys.readouterr()
    surfaces.extend((caplog.text, captured.out, captured.err))

    assert any("leaky failed with ToolContext(" in text for text in surfaces)
    assert "agent_run_failed" in caplog.text + captured.out + captured.err  # the failure was logged
    assert any(span.events for span in exporter.get_finished_spans())  # exceptions were recorded
    for text in surfaces:
        assert SECRET not in text
        assert NEWER not in text
    assert runtime._credentials == {}


async def test_child_run_sees_the_parents_credentials() -> None:
    seen = Seen()
    provider = MockModelProvider(
        [
            tool_call_response("core.subagent", {"agent": "child-agent", "input": "look"}),
            tool_call_response("api.read", {}),
            "child done",
            "parent done",
        ]
    )
    runtime = make_runtime(
        provider,
        make_agent(
            enabled_tools=frozenset({"core.subagent"}),
            tool_permissions=frozenset({"agents.delegate"}),
        ),
    )
    runtime.agents.register(make_agent(name="child-agent", enabled_tools=frozenset({"api.read"})))
    runtime.tools.register(subagent_tool(runtime))
    held: list[set[str]] = []
    reader = seen.tool("api.read")

    async def read(arguments: dict[str, Any], context: ToolContext) -> Any:
        held.append(set(runtime._credentials))
        return await reader.handler(arguments, context)  # type: ignore[misc]

    runtime.tools.register(Tool(reader.definition, read))

    result = await runtime.run(request(), credentials={"authorization": SECRET})

    assert result.status == RunStatus.COMPLETED, result.error
    assert result.output == "parent done"
    [(child_run_id, child_credentials)] = seen.calls
    assert child_run_id != result.run_id
    assert child_credentials == {"authorization": SECRET}
    assert held == [{result.run_id, child_run_id}]  # the child's own entry, beside the parent's
    assert runtime._credentials == {}


async def test_parallel_tool_calls_all_see_the_same_credentials() -> None:
    seen = Seen()
    runtime, _ = runtime_with(
        seen,
        parallel_reply("api.read", 3),
        "done",
        tool_policy=ToolPolicy(parallel_calls=True),
        delay=0.01,
    )

    result = await runtime.run(request(), credentials={"authorization": SECRET})

    assert result.status == RunStatus.COMPLETED, result.error
    assert seen.credentials == [{"authorization": SECRET}] * 3


async def test_concurrent_runs_never_see_each_others_credentials() -> None:
    seen = Seen()
    runs = 6
    runtime, _ = runtime_with(
        seen, *([tool_call_response("api.read", {})] * runs), *(["done"] * runs), delay=0.01
    )

    async def one(index: int) -> tuple[str, str]:
        state = await runtime.start(
            request(user_id=f"user-{index}"), credentials={"authorization": f"Bearer {index}"}
        )
        return state.id, f"Bearer {index}"

    started = await asyncio.gather(*(one(index) for index in range(runs)))
    for run_id, _ in started:
        result = await runtime.wait(run_id, "t")
        assert result.status == RunStatus.COMPLETED, result.error

    expected = dict(started)
    assert len(seen.calls) == runs
    for run_id, credentials in seen.calls:
        assert credentials == {"authorization": expected[run_id]}
    assert runtime._credentials == {}
