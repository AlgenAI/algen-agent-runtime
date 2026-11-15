from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from algen_agent_runtime.config.registry import pinned_agent
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
# Also on a child's metadata: the run at the top of the delegation chain, and the parent's
# approval and guardrail settings the child runs with at least (see delegated_agent).
_ROOT_KEY = "root_run_id"
_FLOOR_KEY = "delegation_floor"
# Tools that act only on the run's own context: a child may enable them although its parent
# does not, since they reach nothing outside the child run.
_RUN_LOCAL_TOOLS = frozenset({"core.working_state_update", "core.skills_load", "core.context_read"})
# Lineages whose delegations this process counts; the oldest are forgotten past this.
_TRACKED_LINEAGES = 10_000
_INJECTION_ORDER = {"off": 0, "flag": 1, "block": 2, "drop": 2}


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


def _floor(agent: AgentDefinition) -> dict[str, Any]:
    """The approval and guardrail settings of ``agent`` a delegated child runs with at least."""
    guardrails = agent.guardrail_policy
    approval = agent.approval_policy
    return {
        "require_for_side_effects": approval.require_for_side_effects,
        "require_for_destructive": approval.require_for_destructive,
        "policies": list(guardrails.policies),
        "tool_policies": list(
            guardrails.policies if guardrails.tool_policies is None else guardrails.tool_policies
        ),
        "tool_result_injection": guardrails.tool_result_injection,
        "retrieval_injection": guardrails.retrieval_injection,
        "pii_exempt": sorted(guardrails.pii.exempt_entities),
    }


def _stricter(child: str, parent: Any) -> Any:
    if not isinstance(parent, str) or parent not in _INJECTION_ORDER:
        return child
    return parent if _INJECTION_ORDER[parent] > _INJECTION_ORDER.get(child, 0) else child


def delegated_agent(agent: AgentDefinition, state: RunState) -> AgentDefinition:
    """``agent`` as it runs ``state``: for a run ``core.subagent`` delegated (0.2.0b4), with the
    delegating agent's approval requirements and guardrail policies added to its own, so a child
    never runs with weaker approvals or fewer guardrails than its parent. Any other run gets
    ``agent`` unchanged.

    Approvals: ``require_for_side_effects`` and ``require_for_destructive`` hold when either agent
    sets them. Guardrails: the
    parent's ``policies`` and ``tool_policies`` join the child's, ``tool_result_injection`` and
    ``retrieval_injection`` take the stricter setting, and only the PII kinds both agents exempt
    stay exempt.
    """
    raw = state.request.metadata.get(_FLOOR_KEY)
    if not raw or not _delegated(state):
        return agent
    try:
        floor = json.loads(raw)
    except ValueError:
        return agent
    if not isinstance(floor, dict):
        return agent
    approval = agent.approval_policy.model_copy(
        update={
            "require_for_side_effects": agent.approval_policy.require_for_side_effects
            or bool(floor.get("require_for_side_effects")),
            "require_for_destructive": agent.approval_policy.require_for_destructive
            or bool(floor.get("require_for_destructive")),
        }
    )
    guardrails = agent.guardrail_policy
    parent_policies = [str(name) for name in floor.get("policies") or ()]
    policies = (
        *guardrails.policies,
        *(name for name in parent_policies if name not in guardrails.policies),
    )
    parent_tool_policies = [str(name) for name in floor.get("tool_policies") or ()]
    own_tool_policies = (
        guardrails.policies if guardrails.tool_policies is None else (guardrails.tool_policies)
    )
    tool_policies: tuple[str, ...] | None
    if guardrails.tool_policies is None and set(parent_tool_policies) <= set(policies):
        tool_policies = None  # the merged policies, which already hold the parent's
    else:
        tool_policies = (
            *own_tool_policies,
            *(name for name in parent_tool_policies if name not in own_tool_policies),
        )
    exempt = guardrails.pii.exempt_entities & frozenset(floor.get("pii_exempt") or ())
    merged_guardrails = guardrails.model_copy(
        update={
            "policies": policies,
            "tool_policies": tool_policies,
            "tool_result_injection": _stricter(
                guardrails.tool_result_injection, floor.get("tool_result_injection")
            ),
            "retrieval_injection": _stricter(
                guardrails.retrieval_injection, floor.get("retrieval_injection")
            ),
            "pii": guardrails.pii.model_copy(update={"exempt_entities": exempt}),
        }
    )
    return agent.model_copy(
        update={"approval_policy": approval, "guardrail_policy": merged_guardrails}
    )


def subagent_tool(runtime: Runtime, max_depth: int = 3, max_descendants: int = 64) -> Tool:
    """``core.subagent``: delegate a bounded task to another agent of the tenant.

    The calling agent may delegate only to the agents its ``delegates`` names (``"*"`` for any),
    and only to an agent whose ``tool_permissions`` and ``enabled_tools`` are within its own
    (0.2.0b4: ``enabled_tools`` too, except the run-local ``core.working_state_update``,
    ``core.skills_load`` and ``core.context_read``), so delegation never widens what a run may
    do. The child runs with the parent's approval requirements and guardrail policies added to
    its own (:func:`delegated_agent`). The nesting depth is counted from the run's lineage
    (``RunRequest.parent_run_id``), never taken from the model, and a delegation is never served
    from the tool-result cache. A whole delegation tree starts at most ``max_descendants`` child
    runs (0.2.0b4; counted by this process), and the Runtime charges each finished child's token
    usage and tool calls to its parent, so the parent's budget bounds the tree. The child runs
    for the same tenant and user, with the parent's credentials.

    When the child pauses for an approval or a clarification, the parent pauses with it: the
    parent's ``approval.required`` or ``clarification.required`` carries the child's pause with
    ``child_run_id``, and answering it on the parent (or on the child) continues the child, then
    the parent with the child's final result.
    """

    # Child runs started per delegation tree (by its root run id), in this process.
    lineages: OrderedDict[str, int] = OrderedDict()

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
        # The parent as it runs: the definition it started with (never a version registered
        # since), with its own delegating parent's settings, when it has one.
        parent_agent = delegated_agent(
            pinned_agent(registry, parent)
            or registry.get(parent.request.agent, parent.request.agent_version),
            parent,
        )
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
        wider_tools = child_agent.enabled_tools - parent_agent.enabled_tools - _RUN_LOCAL_TOOLS
        if wider_tools:
            raise PolicyDeniedError(
                f"agent {name!r} enables tools {parent_agent.name!r} does not: "
                f"{sorted(wider_tools)}; a delegate may use only tools its delegating agent has"
            )
        root = (
            parent.request.metadata.get(_ROOT_KEY) or parent.request.parent_run_id or parent.id
            if _delegated(parent)
            else parent.id
        )
        started = lineages.pop(root, 0)
        lineages[root] = started  # most recently used last
        while len(lineages) > _TRACKED_LINEAGES:
            lineages.popitem(last=False)
        if started >= max_descendants:
            raise PolicyDeniedError(
                f"sub-agent limit reached: this delegation tree already started {started} "
                f"child runs (runtime.subagent.max_descendants is {max_descendants})"
            )
        lineages[root] = started + 1
        result = await runtime.run(
            RunRequest(
                agent=name,
                # The version checked above, so the child runs the definition it was allowed.
                agent_version=child_agent.version,
                input=arguments["input"],
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                parent_run_id=context.run_id,
                metadata={
                    _DELEGATION_KEY: SUBAGENT_TOOL,
                    # Kept for readers of 0.2.0b1 metadata; the lineage is parent_run_id.
                    "parent_run_id": context.run_id,
                    "depth": str(depth + 1),
                    _ROOT_KEY: root,
                    _FLOOR_KEY: json.dumps(_floor(parent_agent), sort_keys=True),
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
