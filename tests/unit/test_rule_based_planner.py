"""RuleBasedPlanner asks for input when a run has none, and takes the clarification as it."""

from __future__ import annotations

from conftest import make_agent, make_runtime

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.planning.contracts import ActionType
from algen_agent_runtime.planning.planners import RuleBasedPlanner
from algen_agent_runtime.types.contracts import Message, Role, RunRequest, RunState, RunStatus


def _state(text: str = "") -> RunState:
    return RunState(request=RunRequest(agent="a", input=text, tenant_id="t", user_id="u"))


async def test_empty_input_asks_for_clarification() -> None:
    state = _state()
    state.messages = [Message.text(Role.SYSTEM, "sys"), Message.text(Role.USER, "")]
    plan = await RuleBasedPlanner().plan(state, make_agent())
    assert plan.actions[0].type == ActionType.CLARIFY


async def test_a_clarification_answer_is_the_input() -> None:
    state = _state()
    state.messages = [
        Message.text(Role.SYSTEM, "sys"),
        Message.text(Role.USER, ""),
        Message.text(Role.USER, "summarise my open tickets"),
    ]
    plan = await RuleBasedPlanner().plan(state, make_agent())
    assert plan.actions[0].type == ActionType.MODEL


async def test_an_earlier_turn_is_not_an_answer() -> None:
    state = _state()
    state.messages = [
        Message.text(Role.USER, "an earlier question"),
        Message.text(Role.ASSISTANT, "an earlier answer"),
        Message.text(Role.USER, ""),
    ]
    plan = await RuleBasedPlanner().plan(state, make_agent())
    assert plan.actions[0].type == ActionType.CLARIFY


async def test_the_run_continues_after_the_clarification() -> None:
    provider = MockModelProvider(["Here are your tickets."])
    runtime = make_runtime(provider, make_agent(planning_strategy="rule_based"))
    request = RunRequest(agent="test-agent", input="", tenant_id="t", user_id="u")

    paused = await runtime.run(request)
    assert paused.status == RunStatus.AWAITING_CLARIFICATION

    await runtime.resume(paused.run_id, "t", {"clarification": "summarise my open tickets"})
    result = await runtime.wait(paused.run_id, "t")
    assert result.status == RunStatus.COMPLETED
    assert result.output == "Here are your tickets."
    assert any(
        message.text_content == "summarise my open tickets"
        for message in provider.requests[-1].messages
    )
