"""Scripted agent runs for the adversarial suite: a mock model that plays the attacker, real
Runtime controls, in-memory stores, no network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelResponse,
    Role,
    RunRequest,
    ToolCall,
)

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"


class Recorder:
    """A tool handler that records each call and returns ``result`` (or ``result(arguments,
    context)``)."""

    def __init__(self, result: Any = None) -> None:
        self.result = {"ok": True} if result is None else result
        self.calls: list[dict[str, Any]] = []
        self.contexts: list[ToolContext] = []

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> Any:
        self.calls.append(arguments)
        self.contexts.append(context)
        if callable(self.result):
            value = self.result(arguments, context)
            if hasattr(value, "__await__"):
                return await value
            return value
        return self.result


def tool(
    name: str,
    side_effect: SideEffect = SideEffect.READ,
    result: Any = None,
    *,
    schema: dict[str, Any] | None = None,
) -> tuple[Tool, Recorder]:
    recorder = Recorder(result)
    definition = ToolDefinition(
        name=name,
        version="1",
        description=name,
        side_effect=side_effect,
        input_schema=schema or {"type": "object"},
        output_schema={"type": "object"},
    )
    return Tool(definition, recorder), recorder


def calls(*items: tuple[str, dict[str, Any]] | ToolCall) -> ModelResponse:
    """One model reply requesting these tool calls."""
    tool_calls = tuple(
        item if isinstance(item, ToolCall) else ToolCall(name=item[0], arguments=item[1])
        for item in items
    )
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, ""),
        tool_calls=tool_calls,
        finish_reason=FinishReason.TOOL_CALLS,
        model="deterministic",
        provider="mock",
    )


def build(
    replies: Iterable[str | ModelResponse] = (),
    tools: Iterable[Tool] = (),
    *,
    provider: MockModelProvider | None = None,
    **agent: Any,
) -> tuple[AgentRuntime, MockModelProvider]:
    tools = tuple(tools)
    provider = provider or MockModelProvider(list(replies))
    agent.setdefault("enabled_tools", frozenset(item.definition.name for item in tools))
    agent.setdefault("max_steps", 20)
    extra: dict[str, Any] = {}
    for key in ("context_artifacts", "learned_memory_store"):
        if key in agent:
            extra[key] = agent.pop(key)
    runtime = make_runtime(provider, make_agent(**agent), **extra)
    for item in tools:
        runtime.tools.register(item)
    return runtime, provider


def request(text: str = "go", *, tenant: str = TENANT, **updates: Any) -> RunRequest:
    return RunRequest(agent="test-agent", input=text, tenant_id=tenant, user_id="user", **updates)


def seen_by_model(provider: MockModelProvider) -> str:
    """Everything every model request carried, as one string."""
    return "\n".join(request.model_dump_json() for request in provider.requests)


async def everything_recorded(runtime: AgentRuntime, run_id: str, tenant: str = TENANT) -> str:
    """The run's events, audits, approvals and narrative, as one string."""
    events = await runtime.events.history(run_id)
    audits = await runtime.audits.list(tenant)
    approvals = await runtime.approvals.list(tenant, run_id=run_id)
    state = await runtime.status(run_id, tenant)
    narrative = await runtime.narrative(run_id, tenant)
    return "\n".join(
        [
            *(event.model_dump_json() for event in events),
            *(audit.model_dump_json() for audit in audits),
            *(approval.model_dump_json() for approval in approvals),
            json.dumps(state.pause_payload or {}),
            str(state.error or ""),
            narrative.model_dump_json(),
        ]
    )


def assert_absent(haystack: str, *needles: str, where: str = "") -> None:
    for needle in needles:
        assert needle not in haystack, f"{needle!r} leaked{' into ' + where if where else ''}"


Handler = Callable[[dict[str, Any], ToolContext], Any]
