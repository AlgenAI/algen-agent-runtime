"""Check that every component an agent names is registered, and suggest the closest name."""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from algen_agent_runtime.types.contracts import AgentDefinition

if TYPE_CHECKING:
    from algen_agent_runtime.orchestration.describe import ComponentInventory


def mcp_tool_prefix(server_name: str, prefix: str | None) -> str:
    """The prefix the MCP client gives every tool it discovers on a server."""
    return (prefix if prefix is not None else f"mcp.{server_name}.").lower().replace("-", "_")


def closest(name: str, candidates: Iterable[str]) -> str | None:
    matches = difflib.get_close_matches(name, sorted(set(candidates)), n=1, cutoff=0.6)
    return matches[0] if matches else None


@dataclass(frozen=True)
class ComponentProblem:
    """One name an agent uses that no source registered."""

    agent: str
    field: str
    name: str
    suggestion: str | None = None

    @property
    def message(self) -> str:
        text = f"agent {self.agent!r}: {self.field} {self.name!r} is not registered"
        return f"{text}; did you mean {self.suggestion!r}?" if self.suggestion else text


@dataclass(frozen=True)
class AvailableComponents:
    """The names a container registers, by the agent field that refers to them.

    Tools under a ``deferred_tool_prefixes`` entry come from MCP servers and are checked when the
    container connects, not here.
    """

    tools: frozenset[str] = frozenset()
    planners: frozenset[str] = frozenset()
    response_composers: frozenset[str] = frozenset()
    verifiers: frozenset[str] = frozenset()
    context_builders: frozenset[str] = frozenset()
    providers: frozenset[str] = frozenset()
    policies: frozenset[str] = frozenset()
    deferred_tool_prefixes: tuple[str, ...] = field(default=())

    @classmethod
    def from_inventory(cls, inventory: ComponentInventory) -> AvailableComponents:
        return cls(
            tools=frozenset(tool.name for tool in inventory.tools),
            planners=frozenset(item.name for item in inventory.planners),
            response_composers=frozenset(item.name for item in inventory.response_composers),
            verifiers=frozenset(item.name for item in inventory.verifiers),
            context_builders=frozenset(item.name for item in inventory.context_builders),
            providers=frozenset(item.name for item in inventory.providers),
            policies=frozenset(item.name for item in inventory.policies),
            deferred_tool_prefixes=tuple(
                mcp_tool_prefix(server.name, server.prefix) for server in inventory.mcp_servers
            ),
        )

    def is_deferred(self, tool: str) -> bool:
        return bool(self.deferred_tool_prefixes) and tool.lower().startswith(
            self.deferred_tool_prefixes
        )


def unknown_agent_components(
    agents: Sequence[AgentDefinition], available: AvailableComponents
) -> tuple[ComponentProblem, ...]:
    """Every tool, planner, composer, verifier, context builder, guardrail policy and provider an
    agent names that ``available`` lacks, in agent order."""
    problems: list[ComponentProblem] = []

    def check(agent: str, field_name: str, name: str, names: frozenset[str]) -> None:
        if name not in names:
            problems.append(ComponentProblem(agent, field_name, name, closest(name, names)))

    for agent in agents:
        for tool in sorted(agent.enabled_tools):
            if not available.is_deferred(tool):
                check(agent.name, "enabled_tools", tool, available.tools)
        check(agent.name, "planning_strategy", agent.planning_strategy, available.planners)
        check(
            agent.name,
            "response_composer",
            agent.response_composer,
            available.response_composers,
        )
        for verifier in agent.verification_policy.verifiers:
            check(agent.name, "verification_policy.verifiers", verifier, available.verifiers)
        check(agent.name, "context_builder", agent.context_builder, available.context_builders)
        # The policy engine skips a name it does not know, so an unregistered guardrail would
        # silently not run.
        for policy in agent.guardrail_policy.policies:
            check(agent.name, "guardrail_policy.policies", policy, available.policies)
        for policy in agent.guardrail_policy.tool_policies or ():
            check(agent.name, "guardrail_policy.tool_policies", policy, available.policies)
        profiles = (
            ("default_model.provider", agent.default_model),
            *(
                (f"fallback_models[{index}].provider", profile)
                for index, profile in enumerate(agent.fallback_models)
            ),
        )
        for field_name, profile in profiles:
            if profile.provider is not None:
                check(agent.name, field_name, profile.provider, available.providers)
    return tuple(problems)


def unknown_mcp_tools(
    agents: Sequence[AgentDefinition], prefix: str, discovered: Iterable[str]
) -> tuple[ComponentProblem, ...]:
    """Enabled tools under one MCP server's ``prefix`` that its discovery did not return."""
    names = frozenset(discovered)
    return tuple(
        ComponentProblem(agent.name, "enabled_tools", tool, closest(tool, names))
        for agent in agents
        for tool in sorted(agent.enabled_tools)
        if tool.lower().startswith(prefix) and tool not in names
    )
