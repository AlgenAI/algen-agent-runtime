from __future__ import annotations

import os
import re
import sys
from typing import Any

import pytest

from algen_agent_runtime.approvals.service import InMemoryApprovalService
from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.context.artifacts import ContextArtifactService
from algen_agent_runtime.context.builder import ContextBuilderRegistry, DefaultContextBuilder
from algen_agent_runtime.events.bus import InMemoryEventBus
from algen_agent_runtime.memory.contracts import LearnedMemoryStore
from algen_agent_runtime.models.base import ModelRouter
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.persistence.memory import InMemoryMemoryStore, InMemoryRunStore
from algen_agent_runtime.planning.planners import PlannerRegistry
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.responses.composer import ResponseComposerRegistry
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import AgentDefinition, ModelProfile
from algen_agent_runtime.verification.verifiers import VerificationService


def make_agent(**updates: Any) -> AgentDefinition:
    values = {
        "name": "test-agent",
        "version": "1.0.0",
        "description": "deterministic test agent",
        "system_instructions": "Be deterministic.",
        "default_model": ModelProfile(name="test", provider="mock", model="deterministic"),
        "planning_strategy": "react",
        "max_steps": 8,
    }
    values.update(updates)
    return AgentDefinition(**values)


def make_runtime(
    provider: Any | None = None,
    agent: AgentDefinition | None = None,
    *,
    context_artifacts: ContextArtifactService | None = None,
    learned_memory_store: LearnedMemoryStore | None = None,
) -> AgentRuntime:
    agents = InMemoryAgentRegistry()
    agents.register(agent or make_agent())
    memory = InMemoryMemoryStore()
    tools = ToolRegistry()
    policies = CompositePolicyEngine()
    router = ModelRouter(circuit_failure_threshold=1)
    router.register_provider(provider or MockModelProvider(["answer"]))
    return AgentRuntime(
        agents=agents,
        router=router,
        tools=tools,
        tool_executor=ToolExecutor(tools, policies),
        planners=PlannerRegistry(),
        contexts=ContextBuilderRegistry(DefaultContextBuilder(memory)),
        policies=policies,
        verifiers=VerificationService(),
        composers=ResponseComposerRegistry(),
        runs=InMemoryRunStore(),
        memory=memory,
        events=InMemoryEventBus(),
        approvals=InMemoryApprovalService(),
        context_artifacts=context_artifacts,
        learned_memory_store=learned_memory_store,
    )


@pytest.fixture
def runtime() -> AgentRuntime:
    return make_runtime()


# The performance suite (tests/perf, marker ``perf``) runs only when asked for: ``-m perf`` (or
# ALGEN_PERF=1). A plain ``pytest``, and every CI job, collects and skips it.
_PERF_SELECTED = re.compile(r"(?<!not )\bperf\b")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    expression = str(config.getoption("markexpr") or "")
    if os.environ.get("ALGEN_PERF") == "1" or _PERF_SELECTED.search(expression):
        return
    skip = pytest.mark.skip(reason="the performance suite runs with -m perf")
    for item in items:
        if item.get_closest_marker("perf") is not None:
            item.add_marker(skip)


# The live provider contract suite (tests/live) writes a results table with ALGEN_LIVE_REPORT.
# The hooks live here: a conftest.py in tests/live would shadow this one for ``from conftest
# import ...``.
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if "tests/live/" in report.nodeid.replace("\\", "/"):
        from live_report import record

        record(report)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if (live_report := sys.modules.get("live_report")) is not None:
        live_report.write()
