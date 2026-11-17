"""The long soak's plugin: scripted model providers with latency, its tools, and a stats probe.

Loaded by the Runtime under test (``domain_plugins: ["soak_plugin:SoakPlugin"]``, with this
directory on ``PYTHONPATH``); see ``README.md`` next to it. Nothing here calls a real model or
service.

- ``soak-fast`` answers in ``SOAK_FAST_MS`` (default 150) milliseconds, jittered, and
  ``soak-slow`` in ``SOAK_SLOW_MS`` (default 3000). Both decide from the conversation alone: the
  last user message lists the tools to call in order (``do: lookup,record``); once each has a
  result, they answer.
- ``soak.lookup`` reads (5 to 40 ms). ``soak.record`` is a write: it appends one line per call to
  ``$SOAK_EFFECTS_DIR/effects-<pid>.jsonl`` with its idempotency key, so the soak can count the
  effects against the tool-execution ledger.
- A probe writes the process's asyncio task count, the Runtime's own task sets, GC counts and
  thread count to ``$SOAK_STATS_DIR/runtime-<pid>.jsonl`` every ``SOAK_STATS_SECONDS`` (30).
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import random
import re
import threading
import time
from typing import Any

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    TokenUsage,
    ToolCall,
)

LOOKUP = "soak.lookup"
RECORD = "soak.record"
_NAMES = {"lookup": LOOKUP, "record": RECORD}
_PLAN = re.compile(r"do:\s*([a-z,]+)")


def _plan(text: str) -> list[str]:
    """``do: lookup,record`` anywhere in the text: the tools to call, in order."""
    found = _PLAN.search(text)
    if found is None:
        return []
    return [_NAMES[name] for name in found.group(1).split(",") if name in _NAMES]


def scripted_reply(request: ModelRequest, provider: str) -> ModelResponse:
    users = [m for m in request.messages if m.role is Role.USER]
    plan = _plan(users[-1].text_content) if users else []
    # Tool results since the last user message: which steps of the plan are done.
    done = 0
    for message in reversed(request.messages):
        if message.role is Role.USER:
            break
        if message.role is Role.TOOL:
            done += 1
    available = {spec.name for spec in request.tools}
    usage = TokenUsage(input_tokens=40 + 4 * len(request.messages), output_tokens=12)
    if done < len(plan) and plan[done] in available:
        call = ToolCall(name=plan[done], arguments={"item": f"step-{done}", "note": "soak"})
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, ""),
            tool_calls=(call,),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=usage,
            model=request.model or "scripted",
            provider=provider,
        )
    text = f"Done: {done} of {len(plan)} steps. " + "All good here. " * random.randint(2, 12)
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, text.strip()),
        finish_reason=FinishReason.STOP,
        usage=usage,
        model=request.model or "scripted",
        provider=provider,
    )


class ScriptedProvider(MockModelProvider):
    """A scripted model with a jittered latency (log-normal around ``latency_ms``)."""

    def __init__(self, provider_id: str, latency_ms: float) -> None:
        super().__init__()
        self.provider_id = provider_id
        self._latency = latency_ms / 1000

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self._latency:
            await asyncio.sleep(
                min(self._latency * 4, random.lognormvariate(0, 0.35) * self._latency)
            )
        return scripted_reply(request, self.provider_id)


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, "") or default)


async def _lookup(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    await asyncio.sleep(random.uniform(0.005, 0.04))
    return {"item": arguments.get("item"), "found": True, "tenant": context.tenant_id}


def _effects_path() -> str:
    directory = os.environ.get("SOAK_EFFECTS_DIR", "/tmp/soak-effects")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, f"effects-{os.getpid()}.jsonl")


async def _record(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    await asyncio.sleep(random.uniform(0.002, 0.02))
    line = json.dumps(
        {
            "key": context.idempotency_key,
            "run_id": context.run_id,
            "tenant": context.tenant_id,
            "at": time.time(),
        }
    )
    # One write(2) of a short line with O_APPEND: whole, and kept by the kernel even if the
    # process is killed right after.
    fd = os.open(_effects_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    return {"recorded": True, "item": arguments.get("item")}


_SCHEMA = {
    "type": "object",
    "properties": {"item": {"type": "string"}, "note": {"type": "string"}},
    "additionalProperties": False,
}


def lookup_tool() -> Tool:
    return Tool(
        ToolDefinition(
            name=LOOKUP,
            version="1",
            description="Look an item up.",
            input_schema=_SCHEMA,
            output_schema={"type": "object"},
            side_effect=SideEffect.READ,
            volatile=True,
            max_concurrency=1_000,
        ),
        _lookup,
    )


def record_tool() -> Tool:
    return Tool(
        ToolDefinition(
            name=RECORD,
            version="1",
            description="Record an item (a write the soak counts).",
            input_schema=_SCHEMA,
            output_schema={"type": "object"},
            side_effect=SideEffect.WRITE,
            idempotency=Idempotency.NON_IDEMPOTENT,
            max_concurrency=1_000,
        ),
        _record,
    )


class StatsProbe:
    """Writes the process's task and GC numbers to a file every few seconds."""

    def __init__(self, container: Any = None) -> None:
        self._task: asyncio.Task[None] | None = None
        self.container = container

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._loop())

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        directory = os.environ.get("SOAK_STATS_DIR", "/tmp/soak-stats")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"runtime-{os.getpid()}.jsonl")
        every = _env_float("SOAK_STATS_SECONDS", 30)
        started = time.time()
        while True:
            record = {
                "at": time.time(),
                "pid": os.getpid(),
                "uptime_s": round(time.time() - started, 1),
                "asyncio_tasks": len(asyncio.all_tasks()),
                "threads": threading.active_count(),
                "gc_counts": list(gc.get_count()),
                "gc_objects": len(gc.get_objects()) if os.environ.get("SOAK_GC_OBJECTS") else None,
            }
            _append(path, json.dumps(record))
            await asyncio.sleep(every)


def _append(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


@plugin(PluginManifest(name="soak", version="1.0.0", publisher="algen-soak"))
class SoakPlugin:
    def register(self, context: Any) -> None:
        fast = ScriptedProvider("soak-fast", _env_float("SOAK_FAST_MS", 150))
        slow = ScriptedProvider("soak-slow", _env_float("SOAK_SLOW_MS", 3000))
        context.providers.register(fast, requests_per_minute=1_000_000, registration_id="soak-fast")
        context.providers.register(slow, requests_per_minute=1_000_000, registration_id="soak-slow")
        context.tools.register(lookup_tool())
        context.tools.register(record_tool())
        context.add_resource(StatsProbe())
