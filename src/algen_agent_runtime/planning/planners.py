from __future__ import annotations

from algen_agent_runtime.context.packing import REPAIR_MESSAGE_NAME
from algen_agent_runtime.planning.contracts import ActionType, Plan, PlannedAction
from algen_agent_runtime.types.contracts import AgentDefinition, Role, RunState
from algen_agent_runtime.types.interfaces import Planner


def _pending_tool_actions(state: RunState) -> tuple[PlannedAction, ...]:
    return tuple(
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


def _answered(state: RunState) -> bool:
    """A run started with empty input has since been given some: a clarification answer.

    Answers are user messages after the run's own (empty) request; earlier turns of the
    session, before it, do not count.
    """
    history = state.context_history if state.context_history is not None else state.messages
    for message in reversed(history):
        if message.role != Role.USER or message.name == REPAIR_MESSAGE_NAME:
            continue
        if not message.text_content.strip():
            return False  # the run's own request: nothing after it answered
        return True
    return False


class DirectPlanner:
    name = "direct"

    async def plan(self, state: RunState, agent: AgentDefinition) -> Plan:
        calls = _pending_tool_actions(state)
        if calls:
            return Plan(
                actions=calls,
                decision_summary="Execute pending model-requested tool calls before continuing.",
            )
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
        calls = _pending_tool_actions(state)
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
        if not state.request.input.strip() and not _answered(state):
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

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._items))

    def get(self, name: str) -> Planner:
        try:
            return self._items[name]
        except KeyError as exc:
            raise KeyError(f"planner {name!r} is not registered") from exc
