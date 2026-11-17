"""Scenarios for the golden telemetry suite: a Runtime container built from settings, as a
service builds it, with scripted models and tools that carry the canaries, and every sink read
back after the run."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from golden_harness import (
    HELD_CANARIES,
    TENANT,
    USER,
    Telemetry,
    audit_shapes,
    deep_text,
    event_shapes,
    metric_shapes,
    span_shapes,
)

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.metering import MeterCall
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import RunRequest

AGENT = "golden-agent"
CHILD = "golden-child"


class RecordingMeter:
    """A usage meter that allows every call and keeps what it was asked."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> Any:
        self.calls.append(deep_text(call))
        return call

    async def settle(self, ticket: Any, usage: Any) -> None:
        self.calls.append(deep_text(ticket) + deep_text(usage))

    async def release(self, ticket: Any) -> None:
        self.calls.append(deep_text(ticket))


def agent_definition(name: str = AGENT, **updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "description": "golden telemetry agent",
        "system_instructions": "Answer from the tools.",
        "default_model": {"name": "m", "provider": "mock", "model": "deterministic"},
        "enabled_tools": [],
        "max_steps": 8,
    }
    values.update(updates)
    return values


def settings(
    *agents: dict[str, Any],
    include_content: bool = False,
    storage: Mapping[str, Any] | None = None,
    **extra: Any,
) -> AppSettings:
    data: dict[str, Any] = {
        "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
        "agents": list(agents) or [agent_definition()],
        "telemetry": {
            "trace_level": "detailed",
            "include_content": include_content,
            "include_conversation_content": include_content,
        },
        "runtime": {"recover_incomplete_runs": False},
    }
    if storage:
        data["storage"] = dict(storage)
    data.update(extra)
    return AppSettings.model_validate(data)


def tool(
    name: str,
    handler: Any,
    *,
    side_effect: SideEffect = SideEffect.READ,
    properties: Mapping[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> Tool:
    definition: dict[str, Any] = {
        "name": name,
        "version": "1",
        "description": f"{name} for the golden suite.",
        "side_effect": side_effect,
        "input_schema": {
            "type": "object",
            "properties": dict(properties or {"query": {"type": "string"}}),
        },
        "output_schema": {"type": "object"},
    }
    if timeout_seconds is not None:
        definition["timeout_seconds"] = timeout_seconds
    return Tool(ToolDefinition(**definition), handler)


def returning(value: Any) -> Any:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        return value

    return handler


def raising(error: BaseException) -> Any:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        raise error

    return handler


def sleeping(seconds: float) -> Any:
    async def handler(arguments: dict[str, Any], context: ToolContext) -> Any:
        await asyncio.sleep(seconds)
        return {"late": True}

    return handler


def container(
    app: AppSettings,
    tools: Sequence[Tool] = (),
    replies: Sequence[Any] = (),
    *,
    meter: RecordingMeter | None = None,
    environment: Mapping[str, str] | None = None,
    **options: Any,
) -> Container:
    built = build_container(
        app,
        additional_tools=tuple(tools),
        usage_meter=meter,
        environment=environment,
        **options,
    )
    if "mock" in app.providers:
        model = built.runtime.router.provider("mock")
        assert isinstance(model, MockModelProvider)
        model.queue(*replies)
    return built


def request(text: str, *, agent: str = AGENT, **updates: Any) -> RunRequest:
    return RunRequest(agent=agent, input=text, tenant_id=TENANT, user_id=USER, **updates)


async def stores_text(built: Container) -> dict[str, str]:
    """The whole contents of every in-memory store, one string per store."""
    runtime = built.runtime
    stores: dict[str, Any] = {
        "run_store": runtime.runs,
        "event_store": runtime.events,
        "audit_store": runtime.audits,
        "approval_store": runtime.approvals,
        "memory_store": runtime.memory,
        "learned_memory_store": getattr(runtime, "learned_memory", None),
        "tool_execution_ledger": runtime.tool_executor.executions,
        "conversation_store": built.conversations.store,
        "conversation_events": built.conversations.events,
        "conversation_feedback": built.conversations.feedback_store,
        "artifact_store": built.artifacts,
        "workflow_checkpoints": built.workflow_checkpoints,
        "cache": built.cache,
    }
    return {name: deep_text(store) for name, store in stores.items() if store is not None}


async def run_records(built: Container, run_ids: Sequence[str]) -> dict[str, Any]:
    """The runs' events, narratives and states, and the tenant's audits, approvals."""
    runtime = built.runtime
    events: list[Any] = []
    narratives: list[str] = []
    states: list[str] = []
    for run_id in run_ids:
        events.extend(await runtime.events.history(run_id))
        narratives.append((await runtime.narrative(run_id, TENANT)).model_dump_json())
        states.append((await runtime.status(run_id, TENANT)).model_dump_json())
    audits = list(await runtime.audits.list(TENANT))
    approvals = list(await runtime.approvals.list(TENANT))
    return {
        "events": events,
        "narratives": narratives,
        "states": states,
        "audits": audits,
        "approvals": approvals,
    }


async def collect(
    built: Container,
    telemetry: Telemetry,
    run_ids: Sequence[str],
    *,
    meter: RecordingMeter | None = None,
    extra: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Every sink as text, and the golden shape of the telemetry."""
    records = await run_records(built, run_ids)
    sinks = {
        "run_events": "\n".join(event.model_dump_json() for event in records["events"]),
        "run_narrative": "\n".join(records["narratives"]),
        "audit_records": "\n".join(audit.model_dump_json() for audit in records["audits"]),
        "approvals": "\n".join(approval.model_dump_json() for approval in records["approvals"]),
        "spans": telemetry.span_text(),
        "metrics": telemetry.metric_text(),
        "logs": telemetry.log_text(),
        "raw_logs": "\n".join(
            value for value in HELD_CANARIES.values() if value in telemetry.raw_log_text()
        ),
        **{f"store:{name}": text for name, text in (await stores_text(built)).items()},
    }
    if meter is not None:
        sinks["usage_meter"] = "\n".join(meter.calls)
    sinks.update(extra or {})
    shape = {
        "events": event_shapes(records["events"]),
        "spans": span_shapes(telemetry.finished_spans()),
        "audits": audit_shapes(records["audits"]),
        "metrics": metric_shapes(telemetry.metric_points()),
        "logs": telemetry.log_events(),
    }
    return sinks, shape


def dump(value: Any) -> str:
    return json.dumps(value, default=str)


def tool_call(name: str, arguments: dict[str, Any], text: str = "") -> Any:
    """A model reply requesting one tool call, with ``text`` as the reply's own words (what an
    approval shows as the call's rationale)."""
    from algen_agent_runtime.types.contracts import (
        FinishReason,
        Message,
        ModelResponse,
        Role,
        ToolCall,
    )

    return ModelResponse(
        message=Message.text(Role.ASSISTANT, text),
        tool_calls=(ToolCall(name=name, arguments=arguments),),
        finish_reason=FinishReason.TOOL_CALLS,
        model="deterministic",
        provider="mock",
    )
