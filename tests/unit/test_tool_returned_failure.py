"""A tool that returns an expected failure as data reports it: ``ToolResult(failed=True)`` or
``ToolDefinition.failure_field``. The call is recorded and emitted as failed, and the model still
gets the payload to correct itself."""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import make_agent, make_runtime
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from algen_agent_runtime.cache import CachePolicy, CacheScope, CacheService, InMemoryCacheStore
from algen_agent_runtime.exceptions.errors import ToolExecutionError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.runtime.guards import FIX_IT, STOP
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.authoring import tool as typed_tool
from algen_agent_runtime.tools.contracts import (
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionStatus,
    ToolResult,
)
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.tools.testing import fake_tool_context
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    Role,
    RunRequest,
    RunStatus,
    ToolPolicy,
)

FAILURE = {"ok": False, "error": "unknown field 'colour'; did you mean 'color'?"}
NO_APPROVAL = ApprovalPolicy(require_for_side_effects=False, require_for_destructive=False)


def _definition(
    name: str = "config.change",
    *,
    side_effect: SideEffect = SideEffect.WRITE,
    failure_field: str | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1",
        description=name,
        side_effect=side_effect,
        input_schema={"type": "object", "properties": {"key": {"type": "string"}}},
        output_schema={"type": "object"},
        failure_field=failure_field,
    )


class Counting:
    """A handler that returns ``value`` (or a ToolResult of it) and counts its calls."""

    def __init__(self, value: Any) -> None:
        self.value = value
        self.calls = 0

    def __call__(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        self.calls += 1
        return self.value(arguments) if callable(self.value) else self.value


def _runtime(provider: MockModelProvider, *tools: Tool, **agent: Any) -> AgentRuntime:
    agent.setdefault("approval_policy", NO_APPROVAL)
    agent.setdefault("max_steps", 20)
    runtime = make_runtime(
        provider,
        make_agent(enabled_tools=frozenset(tool.definition.name for tool in tools), **agent),
    )
    for tool in tools:
        runtime.tools.register(tool)
    return runtime


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="u")


def _tool_results(provider: MockModelProvider) -> list[Any]:
    return [
        json.loads(message.content[0].text)  # type: ignore[union-attr]
        for message in provider.requests[-1].messages
        if message.role == Role.TOOL
    ]


@pytest.mark.parametrize(
    "tool",
    [
        Tool(_definition(), Counting(ToolResult(value=FAILURE, failed=True))),
        Tool(_definition(failure_field="ok"), Counting(FAILURE)),
    ],
    ids=["ToolResult.failed", "failure_field"],
)
async def test_a_returned_failure_is_recorded_failed_and_reaches_the_model(tool: Tool) -> None:
    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    provider = MockModelProvider([tool_call_response("config.change", {"key": "a"}), "fixed"])
    # Without return_to_model too: the tool chose to give the model this result.
    runtime = _runtime(provider, tool)
    runtime._tracer = tracing.get_tracer("test")  # type: ignore[assignment]

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED and result.output == "fixed"
    assert _tool_results(provider) == [FAILURE]
    handler = tool.handler
    assert isinstance(handler, Counting) and handler.calls == 1  # never retried
    events = await runtime.events.history(result.run_id)
    [completed] = [event for event in events if event.type == "tool.completed"]
    assert completed.data["status"] == "failed"
    assert completed.data["result_chars"] == len(json.dumps(FAILURE))
    audits = await runtime.audits.list("t", "config.change")
    assert [(audit.action, audit.outcome) for audit in audits] == [("tool.call", "failed")]
    [span] = [span for span in exporter.get_finished_spans() if span.name == "agent.tool.call"]
    assert dict(span.attributes or {})["tool.status"] == "failed"
    # The call finished: the ledger has it completed with its failed result, so a replay
    # returns it instead of running the call again.
    state = await runtime.status(result.run_id, "t")
    [call_id] = state.completed_tool_call_ids
    record = await runtime.tool_executor.executions.get(  # type: ignore[attr-defined]
        f"{result.run_id}:{call_id}", "t"
    )
    assert record.status == ToolExecutionStatus.COMPLETED
    assert record.result.failed is True and record.result.value == FAILURE


@pytest.mark.parametrize(
    ("value", "failed"),
    [
        ({"ok": False}, True),
        ({"ok": True}, False),
        ({"ok": 0}, False),
        ({"error": "x"}, False),
        ([False], False),
    ],
)
async def test_failure_field_is_false_only_when_the_field_is_false(
    value: Any, failed: bool
) -> None:
    definition = _definition(failure_field="ok").model_copy(update={"output_schema": {}})
    assert definition.reports_failure(value) is failed
    tools = ToolRegistry()
    tools.register(Tool(definition, Counting(value)))
    executor = ToolExecutor(tools, CompositePolicyEngine())
    returned = await executor.execute(
        "config.change", {}, fake_tool_context(tools.get(definition.name))
    )
    assert returned.failed is failed and returned.value == value


async def test_without_a_failure_field_a_false_ok_is_a_plain_result() -> None:
    tools = ToolRegistry()
    tools.register(Tool(_definition(), Counting(FAILURE)))
    executor = ToolExecutor(tools, CompositePolicyEngine())
    returned = await executor.execute(
        "config.change", {}, fake_tool_context(tools.get("config.change"))
    )
    assert returned.failed is False


async def test_a_returned_tool_result_is_checked_and_keeps_its_metadata() -> None:
    tools = ToolRegistry()
    tools.register(
        Tool(
            _definition(),
            Counting(ToolResult(value={"ok": True}, metadata={"source": "tool"})),
        )
    )
    executor = ToolExecutor(tools, CompositePolicyEngine())
    returned = await executor.execute(
        "config.change", {}, fake_tool_context(tools.get("config.change"))
    )
    assert returned == ToolResult(value={"ok": True}, metadata={"source": "tool"})
    # Its value must fit the output schema like any other result.
    tools.register(Tool(_definition("config.bad"), Counting(ToolResult(value=[1], failed=True))))
    with pytest.raises(ToolExecutionError, match="invalid output"):
        await executor.execute("config.bad", {}, fake_tool_context(tools.get("config.bad")))


async def test_a_replayed_returned_failure_does_not_run_again() -> None:
    handler = Counting(FAILURE)
    tools = ToolRegistry()
    tools.register(Tool(_definition(failure_field="ok"), handler))
    executor = ToolExecutor(tools, CompositePolicyEngine())
    context = fake_tool_context(tools.get("config.change"), idempotency_key="run:call")
    first = await executor.execute("config.change", {}, context)
    again = await executor.execute("config.change", {}, context)
    assert first.failed and again.failed and handler.calls == 1


async def test_a_returned_failure_is_never_cached() -> None:
    cache = CacheService(
        InMemoryCacheStore(),
        {"tool_results": CachePolicy(enabled=True, scope=CacheScope.USER)},
    )
    tools = ToolRegistry()
    outcomes = iter([FAILURE, {"ok": True, "key": "a"}, {"ok": False}])
    handler = Counting(lambda arguments: next(outcomes))
    tools.register(
        Tool(_definition("files.read", side_effect=SideEffect.READ, failure_field="ok"), handler)
    )
    executor = ToolExecutor(tools, CompositePolicyEngine(), cache=cache)
    tool = tools.get("files.read")

    async def call() -> ToolResult:
        return await executor.execute("files.read", {"key": "a"}, fake_tool_context(tool))

    assert (await call()).failed  # not cached: the next identical call runs
    assert (await call()).value == {"ok": True, "key": "a"}
    hit = await call()  # the success was cached
    assert hit.value == {"ok": True, "key": "a"} and not hit.failed
    assert hit.metadata.get("runtime_cache_hit") is True
    assert handler.calls == 2


async def test_a_returned_failure_does_not_reset_refusals_or_feed_the_repeat_guard() -> None:
    def precheck(definition: Any, arguments: Any, earlier: Any) -> str | None:
        return "invalid" if arguments.get("key") == "bad" else None

    change = Tool(
        _definition(failure_field="ok"),
        Counting(lambda arguments: {"ok": arguments.get("key") != "fails"}),
        precheck=precheck,
    )
    read = Tool(
        _definition("files.read", side_effect=SideEffect.READ, failure_field="ok"),
        Counting({"ok": False}),
    )
    provider = MockModelProvider(
        [
            tool_call_response("config.change", {"key": "bad"}),
            tool_call_response("config.change", {"key": "fails"}),
            tool_call_response("files.read", {"key": "r"}),
            tool_call_response("files.read", {"key": "r"}),
            tool_call_response("config.change", {"key": "bad"}),
            "done",
        ]
    )
    runtime = _runtime(
        provider,
        change,
        read,
        tool_policy=ToolPolicy(max_refusals_per_tool=2, repeat_guard=True),
    )
    result = await runtime.run(_request())
    assert result.status == RunStatus.COMPLETED
    results = _tool_results(provider)
    assert results[0]["note"] == FIX_IT
    # The failed read is not remembered: the identical read runs again.
    assert results[2] == results[3] == {"ok": False}
    assert isinstance(read.handler, Counting) and read.handler.calls == 2
    # The failed change did not reset the count: this is the second refusal in a row.
    assert results[-1]["note"] == STOP.format(count=2)


async def test_typed_tool_takes_a_failure_field() -> None:
    @typed_tool(failure_field="ok")
    def lookup(key: str) -> dict[str, Any]:
        """Look a key up."""
        return {"ok": False, "error": f"no {key}"}

    assert lookup.definition.failure_field == "ok"
    tools = ToolRegistry()
    tools.register(lookup)
    executor = ToolExecutor(tools, CompositePolicyEngine())
    returned = await executor.execute("lookup", {"key": "a"}, fake_tool_context(lookup))
    assert returned.failed and returned.value == {"ok": False, "error": "no a"}


def test_an_unset_failure_is_left_out_of_the_stored_result() -> None:
    # 0.1.0a17's ToolResult forbids unknown fields; only a failed result carries the new one.
    assert "failed" not in ToolResult(value={"ok": True}).model_dump()
    assert "failed" not in ToolResult(value=1).model_dump_json()
    assert ToolResult(value=1, failed=True).model_dump()["failed"] is True
    assert ToolResult.model_validate_json(ToolResult(value=1).model_dump_json()).failed is False
