from __future__ import annotations

from typing import Any

import pytest

from algen_agent_runtime.approvals.service import InMemoryApprovalService
from algen_agent_runtime.config.registry import InMemoryAgentRegistry
from algen_agent_runtime.context.builder import ContextBuilderRegistry, DefaultContextBuilder
from algen_agent_runtime.events.bus import InMemoryEventBus
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


def make_runtime(provider: Any | None = None, agent: AgentDefinition | None = None) -> AgentRuntime:
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
    )


@pytest.fixture
def runtime() -> AgentRuntime:
    return make_runtime()
