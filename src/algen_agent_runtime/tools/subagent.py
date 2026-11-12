from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)
from algen_agent_runtime.types.contracts import AgentDefinition, RunRequest, RunState
from algen_agent_runtime.types.interfaces import Runtime

SUBAGENT_TOOL = "core.subagent"
# Set on a child run's metadata by core.subagent, next to RunRequest.parent_run_id. Only a run
# with a parent counts as delegated, and a caller cannot set parent_run_id over HTTP.
_DELEGATION_KEY = "delegation"


@runtime_checkable
class _AgentLookup(Protocol):
    def get(self, name: str, version: str | None = None) -> AgentDefinition: ...


def _delegated(state: RunState) -> bool:
    return (
        state.request.parent_run_id is not None
        and state.request.metadata.get(_DELEGATION_KEY) == SUBAGENT_TOOL
    )


def delegated_run(state: RunState) -> str | None:
    """The run that delegated ``state`` with ``core.subagent``, or None for a run nobody
    delegated (a root run, or a workflow node chained by ``parent_run_id``)."""
    return state.request.parent_run_id if _delegated(state) else None


def subagent_tool(runtime: Runtime, max_depth: int = 3) -> Tool:
    """``core.subagent``: delegate a bounded task to another agent of the tenant.

    The calling agent may delegate only to the agents its ``delegates`` names (``"*"`` for any),
    and only to an agent whose ``tool_permissions`` are within its own, so delegation never
    widens what a run may do. The nesting depth is counted from the run's lineage
    (``RunRequest.parent_run_id``), never taken from the model, and a delegation is never served
    from the tool-result cache. The child runs for the same tenant and user, with the parent's
    credentials.

    When the child pauses for an approval or a clarification, the parent pauses with it: the
    parent's ``approval.required`` or ``clarification.required`` carries the child's pause with
    ``child_run_id``, and answering it on the parent (or on the child) continues the child, then
    the parent with the child's final result.
    """

    async def status(run_id: str, tenant_id: str) -> RunState:
        lookup = getattr(runtime, "status", None)
        if lookup is None:
            raise ConfigurationError(f"{SUBAGENT_TOOL} needs a runtime that reads run status")
        state: RunState = await lookup(run_id, tenant_id)
        return state

    def agents() -> _AgentLookup:
        registry = getattr(runtime, "agents", None)
        if not isinstance(registry, _AgentLookup):
            raise ConfigurationError(f"{SUBAGENT_TOOL} needs a runtime with an agent registry")
        return registry

    async def depth_of(parent: RunState, tenant_id: str) -> int:
        """How many delegations led to ``parent``: 0 for a run nobody delegated."""
        depth, current = 0, parent
        while _delegated(current) and depth < max_depth:
            depth += 1
            assert current.request.parent_run_id is not None
            current = await status(current.request.parent_run_id, tenant_id)
        return depth

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        # A "depth" argument is accepted for compatibility with older prompts and ignored.
        parent = await status(context.run_id, context.tenant_id)
        depth = await depth_of(parent, context.tenant_id)
        if depth >= max_depth:
            raise PolicyDeniedError(f"sub-agent nesting limit {max_depth} reached")
        registry = agents()
        parent_agent = registry.get(parent.request.agent, parent.request.agent_version)
        name = arguments["agent"]
        if "*" not in parent_agent.delegates and name not in parent_agent.delegates:
            raise PolicyDeniedError(
                f"agent {parent_agent.name!r} may not delegate to {name!r}; "
                "list it in the agent's delegates"
            )
        child_agent = registry.get(name, arguments.get("agent_version"))
        wider = child_agent.tool_permissions - parent_agent.tool_permissions
        if wider:
            raise PolicyDeniedError(
                f"agent {name!r} has tool permissions {parent_agent.name!r} lacks: {sorted(wider)}"
            )
        result = await runtime.run(
            RunRequest(
                agent=name,
                agent_version=arguments.get("agent_version"),
                input=arguments["input"],
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                parent_run_id=context.run_id,
                metadata={
                    _DELEGATION_KEY: SUBAGENT_TOOL,
                    # Kept for readers of 0.2.0b1 metadata; the lineage is parent_run_id.
                    "parent_run_id": context.run_id,
                    "depth": str(depth + 1),
                },
            ),
            # The child acts for the same caller: it gets the parent's credentials, in memory,
            # until it pauses or ends.
            credentials=context.credentials,
        )
        return result.model_dump(mode="json")

    return Tool(
        ToolDefinition(
            name=SUBAGENT_TOOL,
            version="1.0.0",
            description="Delegate a bounded task to another registered agent in the same tenant.",
            input_schema={
                "type": "object",
                "properties": {
                    "agent": {"type": "string"},
                    "agent_version": {"type": "string"},
                    "input": {"type": "string", "minLength": 1},
                    # Deprecated and ignored: the Runtime counts the depth from the run's lineage.
                    "depth": {"type": "integer", "minimum": 0},
                },
                "required": ["agent", "input"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["run_id", "status", "session_id"]},
            required_permissions=frozenset({"agents.delegate"}),
            side_effect=SideEffect.NONE,
            # Each call starts a new child run: never retried, cached or held back as a repeat.
            idempotency=Idempotency.NON_IDEMPOTENT,
            volatile=True,
        ),
        execute,
    )
