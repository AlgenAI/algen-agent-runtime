from __future__ import annotations

from algen_agent_runtime.planning.contracts import ActionType, Plan, PlannedAction
from algen_agent_runtime.types.contracts import AgentDefinition, RunState
from algen_agent_runtime.types.interfaces import Planner


class DirectPlanner:
    name = "direct"

    async def plan(self, state: RunState, agent: AgentDefinition) -> Plan:
        action = ActionType.COMPLETE if state.output_text else ActionType.MODEL
        return Plan(
            actions=(
                PlannedAction(
                    id=f"step-{state.step_count + 1}",
                    type=action,
                    description="Compose available output"
                    if state.output_text
                    else "Generate response",
                ),
            ),
            decision_summary="Direct execution selected the next bounded action.",
        )


class ReActPlanner:
    name = "react"

    async def plan(self, state: RunState, agent: AgentDefinition) -> Plan:
        if state.pending_tool_calls:
            calls = tuple(
                PlannedAction(
                    id=f"tool-{call.id}",
                    type=ActionType.TOOL,
                    description=f"Execute requested tool {call.name}",
                    tool_name=call.name,
                    arguments=call.arguments,
                )
                for call in state.pending_tool_calls
                if call.id not in state.completed_tool_call_ids
            )
            if calls:
                return Plan(
                    actions=calls,
                    decision_summary="Execute validated model-requested tool calls.",
                )
        if state.output_text:
            return Plan(
                actions=(
                    PlannedAction(
                        id=f"complete-{state.step_count + 1}",
                        type=ActionType.COMPLETE,
                        description="Finalize verified model output",
                    ),
                ),
                decision_summary="A final model response is available.",
            )
        return await DirectPlanner().plan(state, agent)


class RuleBasedPlanner:
    name = "rule_based"

    async def plan(self, state: RunState, agent: AgentDefinition) -> Plan:
        if not state.request.input.strip():
            return Plan(
                actions=(
                    PlannedAction(
                        id="clarify-input",
                        type=ActionType.CLARIFY,
                        description="Please provide a non-empty request.",
                    ),
                ),
                decision_summary="Input is insufficient to execute.",
            )
        return await ReActPlanner().plan(state, agent)


class PlannerRegistry:
    def __init__(self) -> None:
        items: tuple[Planner, ...] = (DirectPlanner(), ReActPlanner(), RuleBasedPlanner())
        self._items: dict[str, Planner] = {planner.name: planner for planner in items}

    def register(self, planner: Planner) -> None:
        self._items[planner.name] = planner

    def get(self, name: str) -> Planner:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(f"planner {name!r} is not registered") from exc
