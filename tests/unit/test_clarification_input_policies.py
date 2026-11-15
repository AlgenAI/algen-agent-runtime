"""A clarification or core.ask_user answer goes through the input policies, as steering does
(GOV-8)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.ask_user import ASK_USER_PERMISSION, ASK_USER_TOOL
from algen_agent_runtime.types.contracts import Role, RunRequest, RunStatus

ASK = tool_call_response(ASK_USER_TOOL, {"question": "Which account?"})
SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"


class RefuseWord:
    name = "refuse_word"
    category = "input_validation"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.INPUT})

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if isinstance(payload, str) and "forbidden" in payload:
            return PolicyDecision(
                action=PolicyAction.DENY, reason_code="word.refused", reason="Refused word."
            )
        return PolicyDecision(action=PolicyAction.ALLOW, reason_code="ok", reason="Allowed.")


def _runtime(*responses: Any) -> AgentRuntime:
    runtime = make_runtime(
        MockModelProvider(list(responses)),
        make_agent(
            enabled_tools=frozenset({ASK_USER_TOOL}),
            tool_permissions=frozenset({ASK_USER_PERMISSION}),
        ),
    )
    runtime.policies.register(RefuseWord(), mandatory=True)  # type: ignore[attr-defined]
    return runtime


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="check it", tenant_id="t", user_id="u")


async def test_an_ask_user_answer_is_redacted_by_the_input_policies() -> None:
    runtime = _runtime(ASK, "done")
    paused = await runtime.run(_request())
    assert paused.status == RunStatus.AWAITING_CLARIFICATION, paused.error

    await runtime.resume(paused.run_id, "t", {"clarification": f"use api_key={SECRET}"})
    final = await runtime.wait(paused.run_id, "t")

    assert final.status == RunStatus.COMPLETED, final.error
    state = await runtime.status(paused.run_id, "t")
    [answer] = [m for m in state.messages if m.role == Role.TOOL and m.name == ASK_USER_TOOL]
    assert SECRET not in answer.text_content
    assert json.loads(answer.text_content)["answer"] == "use api_key=[REDACTED]"


async def test_a_refused_answer_raises_and_the_run_waits_for_another() -> None:
    runtime = _runtime(ASK, "done")
    paused = await runtime.run(_request())

    with pytest.raises(PolicyDeniedError, match=r"word\.refused"):
        await runtime.resume(paused.run_id, "t", {"clarification": "a forbidden answer"})

    state = await runtime.status(paused.run_id, "t")
    assert state.status == RunStatus.AWAITING_CLARIFICATION
    assert not [m for m in state.messages if m.role == Role.TOOL and m.name == ASK_USER_TOOL]

    await runtime.resume(paused.run_id, "t", {"clarification": "main"})
    assert (await runtime.wait(paused.run_id, "t")).status == RunStatus.COMPLETED


async def test_a_plain_clarification_answer_is_checked_too() -> None:
    runtime = _runtime()
    paused = await runtime.run(_request())
    state = await runtime.status(paused.run_id, "t")
    # A run paused for a clarification the planner asked for (not core.ask_user).
    state.status = RunStatus.AWAITING_CLARIFICATION
    state.pause_payload = {"kind": "clarification", "question": "Which?"}
    await runtime.runs.save(state, state.version)

    with pytest.raises(PolicyDeniedError, match=r"word\.refused"):
        await runtime.resume(paused.run_id, "t", {"clarification": "forbidden"})
    assert (await runtime.status(paused.run_id, "t")).status == RunStatus.AWAITING_CLARIFICATION
