"""Shared pieces of the performance suite (``tests/perf``, marker ``perf``).

A perf test starts a real Runtime container (``build_container`` plus the app lifespan, as
``serve`` runs it) with a deterministic model and a no-op tool, so what it measures is the
Runtime's own work: planning, policy, the tool executor and ledger, events, checkpoints and the
stores. The model answers from the run's input, ``steps=S tools=T``: ``S`` rounds of ``T``
parallel ``perf.noop`` calls, then a final answer. ``ALGEN_PERF_MODEL_DELAY_MS`` adds a simulated
model latency where a test asks for it.

Results are printed (``PERF {json}``) and, when ``ALGEN_PERF_RESULTS`` names a file, appended to
it as JSON lines.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import resource
import sys
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

HERE = Path(__file__).resolve().parent
# The drills' database helpers (a fresh database per test on the server POSTGRES_DSN names).
sys.path.insert(0, str(HERE.parent / "drills"))

from algen_agent_runtime.api.app import create_app  # noqa: E402
from algen_agent_runtime.config.settings import AppSettings  # noqa: E402
from algen_agent_runtime.models.providers import mock as mock_module  # noqa: E402
from algen_agent_runtime.orchestration.container import Container, build_container  # noqa: E402
from algen_agent_runtime.tools.contracts import (  # noqa: E402
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import (  # noqa: E402
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    TokenUsage,
    ToolCall,
)

AGENT = "perf-agent"
TOOL = "perf.noop"
USER = "perf-user"


def headers(tenant: str) -> dict[str, str]:
    """Development header identity: the app runs behind ASGITransport, a loopback client."""
    return {"x-tenant-id": tenant, "x-user-id": USER, "x-scopes": "*"}


def plan(steps: int, tools: int = 1) -> str:
    return f"steps={steps} tools={tools}"


def _parse(text: str) -> tuple[int, int]:
    values = dict(part.split("=", 1) for part in text.split() if "=" in part)
    return int(values.get("steps", "0")), int(values.get("tools", "1"))


def reply(request: ModelRequest) -> ModelResponse:
    """The deterministic model: decide from the messages alone."""
    user = next(m for m in reversed(request.messages) if m.role is Role.USER)
    steps, tools = _parse(user.text_content)
    done = sum(1 for m in request.messages if m.role is Role.TOOL) // max(1, tools)
    usage = TokenUsage(input_tokens=10, output_tokens=3)
    if done < steps and any(spec.name == TOOL for spec in request.tools):
        calls = tuple(
            ToolCall(name=TOOL, arguments={"step": done, "index": index}) for index in range(tools)
        )
        return ModelResponse(
            message=Message.text(Role.ASSISTANT, ""),
            tool_calls=calls,
            finish_reason=FinishReason.TOOL_CALLS,
            usage=usage,
            model=request.model or "deterministic",
            provider="mock",
        )
    return ModelResponse(
        message=Message.text(Role.ASSISTANT, f"done after {done} steps"),
        finish_reason=FinishReason.STOP,
        usage=usage,
        model=request.model or "deterministic",
        provider="mock",
    )


def provider_class(delay_seconds: float) -> type[mock_module.MockModelProvider]:
    class PerfProvider(mock_module.MockModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            return reply(request)

    return PerfProvider


async def _noop(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    return {"ok": True}


def noop_tool() -> Tool:
    return Tool(
        ToolDefinition(
            name=TOOL,
            version="1",
            description="Do nothing and say so.",
            input_schema={
                "type": "object",
                "properties": {"step": {"type": "integer"}, "index": {"type": "integer"}},
            },
            output_schema={"type": "object"},
            side_effect=SideEffect.READ,
            # Each call is distinct and never served from the tool-result cache.
            volatile=True,
            max_concurrency=10_000,
        ),
        _noop,
    )


POSTGRES_STORES = (
    "run_store",
    "memory_store",
    "event_store",
    "audit_store",
    "approval_store",
    "artifact_store",
    "workflow_store",
    "tool_execution_store",
    "conversation_store",
)


def perf_settings(
    stores: str,
    *,
    pool_max: int = 10,
    provider_concurrency: int = 20,
    max_steps: int = 12,
    api: dict[str, Any] | None = None,
) -> AppSettings:
    """``stores``: ``memory``, ``postgres`` (every store) or ``postgres+redis`` (every store
    on PostgreSQL but session memory, which is on Redis)."""
    storage: dict[str, Any] = {}
    if stores.startswith("postgres"):
        storage.update({name: "postgres" for name in POSTGRES_STORES})
        storage["postgres_dsn"] = "env://PERF_POSTGRES_DSN"
        storage["postgres_max_pool_size"] = pool_max
    if stores == "postgres+redis":
        storage["memory_store"] = "redis"
        storage["redis_url"] = "env://PERF_REDIS_URL"
    values: dict[str, Any] = {
        "providers": {
            "mock": {
                "type": "mock",
                "default_model": "deterministic",
                # The provider's own pacing (600 a minute by default) would measure itself.
                "requests_per_minute": 1_000_000,
                "max_concurrency": provider_concurrency,
            }
        },
        "agents": [
            {
                "name": AGENT,
                "version": "1.0.0",
                "description": "Calls a no-op tool as told, then answers",
                "system_instructions": "Call the tool as many times as asked, then report.",
                "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                "enabled_tools": [TOOL],
                "max_steps": max_steps,
                "approval_policy": {"require_for_side_effects": False},
            }
        ],
        "runtime": {"shutdown_grace_seconds": 2},
        "storage": storage,
    }
    if api:
        values["api"] = api
    return AppSettings.model_validate(values)


@contextmanager
def _provider(delay_seconds: float) -> Iterator[None]:
    original = mock_module.MockModelProvider
    mock_module.MockModelProvider = provider_class(delay_seconds)  # type: ignore[misc]
    try:
        yield
    finally:
        mock_module.MockModelProvider = original  # type: ignore[misc]


@dataclass
class Process:
    container: Container
    app: Any
    client: httpx.AsyncClient

    @property
    def runtime(self) -> Any:
        return self.container.runtime

    def pool(self) -> Any:
        for item in self.container.resources:
            if type(item).__name__ == "PostgresDatabase":
                return item._pool
        return None


@asynccontextmanager
async def started(
    settings: AppSettings,
    *,
    dsn: str | None = None,
    redis_url: str | None = None,
    model_delay_seconds: float = 0.0,
) -> AsyncIterator[Process]:
    """A started Runtime process (container, app lifespan, HTTP client); stopped afterwards."""
    environment = {
        **({"PERF_POSTGRES_DSN": dsn} if dsn else {}),
        **({"PERF_REDIS_URL": redis_url} if redis_url else {}),
    }
    with _provider(model_delay_seconds):
        container = build_container(
            settings, environment=environment, additional_tools=[noop_tool()]
        )
    app = create_app(settings, container)
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    client = httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://perf", timeout=300
    )
    try:
        yield Process(container, app, client)
    finally:
        await client.aclose()
        await lifespan.__aexit__(None, None, None)


# --- Numbers -----------------------------------------------------------------------------------


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def summary_ms(values: Sequence[float]) -> dict[str, float]:
    """p50, p95, p99, mean and max of durations in seconds, in milliseconds."""
    return {
        "p50_ms": round(percentile(values, 0.50) * 1000, 2),
        "p95_ms": round(percentile(values, 0.95) * 1000, 2),
        "p99_ms": round(percentile(values, 0.99) * 1000, 2),
        "mean_ms": round(sum(values) / len(values) * 1000, 2) if values else float("nan"),
        "max_ms": round(max(values) * 1000, 2) if values else float("nan"),
    }


def peak_rss_mb() -> float:
    """This process's peak resident set size so far."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return round(peak / (1_000_000 if sys.platform == "darwin" else 1_000), 1)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, "") or default)


def report(kind: str, values: dict[str, Any]) -> None:
    record = {
        "kind": kind,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        **values,
    }
    line = json.dumps(record)
    print("PERF " + line)
    target = os.environ.get("ALGEN_PERF_RESULTS")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
