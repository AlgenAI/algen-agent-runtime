"""A core.subagent child run's approval or clarification pauses its parent, and answering it
on the parent (or on the child) continues the child, then the parent (A4 / G61)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from conftest import make_agent, make_runtime

from algen_agent_runtime.approvals.service import ApprovalStatus
from algen_agent_runtime.exceptions.errors import ConflictError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.tools.subagent import subagent_tool
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    Role,
    RunRequest,
    RunState,
    RunStatus,
)

DELEGATE = {
    "enabled_tools": frozenset({"core.subagent"}),
    "tool_permissions": frozenset({"agents.delegate", "user.ask"}),
}
SEND = tool_call_response("mail.send", {"to": "bob"})
DELEGATION = tool_call_response("core.subagent", {"agent": "child-agent", "input": "mail bob"})


def _runtime(*responses: Any, middle: bool = False, **child: Any) -> tuple[AgentRuntime, list[str]]:
    sent: list[str] = []
    runtime = make_runtime(
        MockModelProvider(list(responses)),
        make_agent(**DELEGATE, delegates=frozenset({"middle-agent" if middle else "child-agent"})),
    )
    runtime.agents.register(
        make_agent(
            name="child-agent",
            enabled_tools=frozenset({"mail.send", "core.ask_user"}),
            tool_permissions=frozenset({"user.ask"}),
            **child,
        )
    )
    if middle:
        runtime.agents.register(
            make_agent(name="middle-agent", **DELEGATE, delegates=frozenset({"child-agent"}))
        )

    def send(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        sent.append(str(arguments.get("to")))
        return {"sent": True}

    runtime.tools.register(
        Tool(
            ToolDefinition(
                name="mail.send",
                version="1",
                description="Send an email",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                side_effect=SideEffect.EXTERNAL,
                requires_approval=True,
            ),
            send,
        )
    )
    runtime.tools.register(subagent_tool(runtime))
    return runtime, sent


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="alice")


def _children(runtime: AgentRuntime, parent_run_id: str) -> list[RunState]:
    runs = runtime.runs._runs.values()  # type: ignore[attr-defined]
    return [run for run in runs if run.request.parent_run_id == parent_run_id]


async def _settled(runtime: AgentRuntime, run_id: str) -> RunState:
    """The run once it ended or paused, and its drive finished."""
    result = await runtime.wait(run_id, "t")
    return await runtime.status(result.run_id, "t")


def _subagent_result(state: RunState) -> dict[str, Any]:
    [message] = [
        message
        for message in state.messages
        if message.role == Role.TOOL and message.name == "core.subagent"
    ]
    value: dict[str, Any] = json.loads(message.text_content)
    return value


async def test_a_childs_approval_pauses_the_parent_with_the_childs_request() -> None:
    runtime, sent = _runtime(DELEGATION, SEND)

    result = await runtime.run(_request())

    assert result.status == RunStatus.AWAITING_APPROVAL, result.error
    parent = await runtime.status(result.run_id, "t")
    [child] = _children(runtime, parent.id)
    assert child.status == RunStatus.AWAITING_APPROVAL
    pause = parent.pause_payload or {}
    child_pause = child.pause_payload or {}
    assert pause["child_run_id"] == child.id
    assert pause["origin_run_id"] == child.id
    assert pause["child_agent"] == "child-agent"
    assert pause["approval_id"] == child_pause["approval_id"]
    assert pause["tool"] == "mail.send"
    assert pause["kind"] == "tool_approval"
    assert parent.delegated_runs == {pause["subagent_call_id"]: child.id}
    events = await runtime.events.history(parent.id)
    assert events[-1].type == "approval.required"
    assert events[-1].data["child_run_id"] == child.id
    assert sent == []


async def test_approving_on_the_parent_runs_the_child_then_the_parent() -> None:
    runtime, sent = _runtime(DELEGATION, SEND, "child sent it", "parent done")
    paused = await runtime.run(_request())
    parent = await runtime.status(paused.run_id, "t")
    approval_id = str((parent.pause_payload or {})["approval_id"])

    await runtime.resume(parent.id, "t", {"decision": "approved"}, decided_by="carol")
    final = await _settled(runtime, parent.id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert final.output_text == "parent done"
    assert sent == ["bob"]
    [child] = _children(runtime, parent.id)
    assert child.status == RunStatus.COMPLETED
    delegated = _subagent_result(final)
    assert delegated["status"] == "completed"
    assert delegated["output"] == "child sent it"
    assert final.delegated_runs == {}
    approval = await runtime.approvals.get(approval_id, "t")
    assert approval is not None
    assert (approval.status, approval.decided_by, approval.run_id) == (
        ApprovalStatus.APPROVED,
        "carol",
        child.id,
    )


async def test_rejecting_on_the_parent_fails_the_child_and_the_parent_hears_it() -> None:
    runtime, sent = _runtime(DELEGATION, SEND, "parent explains")
    paused = await runtime.run(_request())

    await runtime.resume(paused.run_id, "t", {"decision": "rejected", "reason": "no"})
    final = await _settled(runtime, paused.run_id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert sent == []
    [child] = _children(runtime, final.id)
    assert child.status == RunStatus.FAILED
    assert _subagent_result(final)["status"] == "failed"


async def test_a_rejection_the_child_continues_after_reaches_the_parent_too() -> None:
    runtime, sent = _runtime(
        DELEGATION,
        SEND,
        "child: not sent",
        "parent done",
        approval_policy=ApprovalPolicy(on_rejection="continue"),
    )
    paused = await runtime.run(_request())

    await runtime.resume(paused.run_id, "t", {"decision": "rejected"})
    final = await _settled(runtime, paused.run_id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert sent == []
    assert _subagent_result(final)["output"] == "child: not sent"


async def test_deciding_on_the_child_continues_the_waiting_parent() -> None:
    runtime, sent = _runtime(DELEGATION, SEND, "child sent it", "parent done")
    paused = await runtime.run(_request())
    [child] = _children(runtime, paused.run_id)

    await runtime.resume(child.id, "t", {"decision": "approved"}, decided_by="carol")
    final = await _settled(runtime, paused.run_id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert sent == ["bob"]
    assert _subagent_result(final)["output"] == "child sent it"


async def test_a_second_child_approval_pauses_the_parent_again() -> None:
    runtime, sent = _runtime(DELEGATION, SEND, SEND, "child sent twice", "parent done")
    paused = await runtime.run(_request())
    first = str((await runtime.status(paused.run_id, "t")).pause_payload["approval_id"])  # type: ignore[index]

    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    again = await _settled(runtime, paused.run_id)

    assert again.status == RunStatus.AWAITING_APPROVAL
    second = str((again.pause_payload or {})["approval_id"])
    assert second != first
    assert sent == ["bob"]
    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    final = await _settled(runtime, paused.run_id)
    assert final.status == RunStatus.COMPLETED, final.error
    assert sent == ["bob", "bob"]
    assert len(_children(runtime, paused.run_id)) == 1


async def test_a_childs_question_pauses_the_parent_for_the_answer() -> None:
    runtime, _ = _runtime(
        DELEGATION,
        tool_call_response("core.ask_user", {"question": "Which Bob?", "choices": ["A", "B"]}),
        "child used B",
        "parent done",
    )
    paused = await runtime.run(_request())

    assert paused.status == RunStatus.AWAITING_CLARIFICATION, paused.error
    parent = await runtime.status(paused.run_id, "t")
    [child] = _children(runtime, parent.id)
    pause = parent.pause_payload or {}
    assert (pause["question"], pause["choices"], pause["child_run_id"]) == (
        "Which Bob?",
        ["A", "B"],
        child.id,
    )
    assert (await runtime.events.history(parent.id))[-1].type == "clarification.required"

    await runtime.resume(parent.id, "t", {"clarification": "B"})
    final = await _settled(runtime, parent.id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert _subagent_result(final)["output"] == "child used B"
    child = await runtime.status(child.id, "t")
    [answer] = [m for m in child.messages if m.role == Role.TOOL and m.name == "core.ask_user"]
    assert json.loads(answer.text_content) == {"answer": "B"}


async def test_an_answer_of_the_wrong_kind_changes_nothing() -> None:
    runtime, _ = _runtime(DELEGATION, SEND)
    paused = await runtime.run(_request())

    with pytest.raises(ValueError, match="decision"):
        await runtime.resume(paused.run_id, "t", {"clarification": "yes"})

    assert (await runtime.status(paused.run_id, "t")).status == RunStatus.AWAITING_APPROVAL
    [child] = _children(runtime, paused.run_id)
    assert child.status == RunStatus.AWAITING_APPROVAL


async def test_a_pause_two_levels_down_reaches_the_root_and_back() -> None:
    runtime, sent = _runtime(
        tool_call_response("core.subagent", {"agent": "middle-agent", "input": "delegate"}),
        DELEGATION,
        SEND,
        "child sent it",
        "middle done",
        "root done",
        middle=True,
    )
    paused = await runtime.run(_request())

    assert paused.status == RunStatus.AWAITING_APPROVAL, paused.error
    root = await runtime.status(paused.run_id, "t")
    [middle] = _children(runtime, root.id)
    [child] = _children(runtime, middle.id)
    pause = root.pause_payload or {}
    assert (pause["child_run_id"], pause["origin_run_id"], pause["child_agent"]) == (
        middle.id,
        child.id,
        "middle-agent",
    )
    assert pause["approval_id"] == (child.pause_payload or {})["approval_id"]
    assert middle.status == RunStatus.AWAITING_APPROVAL

    await runtime.resume(root.id, "t", {"decision": "approved"}, decided_by="carol")
    final = await _settled(runtime, root.id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert final.output_text == "root done"
    assert sent == ["bob"]
    assert (await runtime.status(middle.id, "t")).status == RunStatus.COMPLETED
    assert (await runtime.status(child.id, "t")).status == RunStatus.COMPLETED
    assert _subagent_result(final)["output"] == "middle done"


async def test_deciding_the_deepest_child_wakes_every_parent() -> None:
    runtime, sent = _runtime(
        tool_call_response("core.subagent", {"agent": "middle-agent", "input": "delegate"}),
        DELEGATION,
        SEND,
        "child sent it",
        "middle done",
        "root done",
        middle=True,
    )
    paused = await runtime.run(_request())
    [middle] = _children(runtime, paused.run_id)
    [child] = _children(runtime, middle.id)

    await runtime.resume(child.id, "t", {"decision": "approved"})
    final = await _settled(runtime, paused.run_id)

    assert final.status == RunStatus.COMPLETED, final.error
    assert sent == ["bob"]


async def test_cancelling_the_parent_cancels_the_waiting_child() -> None:
    runtime, _ = _runtime(DELEGATION, SEND)
    paused = await runtime.run(_request())
    [child] = _children(runtime, paused.run_id)

    await runtime.cancel(paused.run_id, "t")

    assert (await runtime.status(child.id, "t")).status == RunStatus.CANCELLED


async def test_a_stale_parent_pause_is_not_applied_to_another_approval() -> None:
    runtime, _ = _runtime(DELEGATION, SEND)
    paused = await runtime.run(_request())
    parent = await runtime.status(paused.run_id, "t")
    # The child now waits on another approval than the one the parent shows.
    parent.pause_payload = {**(parent.pause_payload or {}), "approval_id": "other"}
    await runtime.runs.save(parent, parent.version)

    with pytest.raises(ConflictError, match="read the run again"):
        await runtime.resume(parent.id, "t", {"decision": "approved"})


async def test_the_parent_waits_with_the_child_for_the_answer() -> None:
    # A parent's deadline is moved on by its pause, like any paused run's.
    runtime, _ = _runtime(DELEGATION, SEND, "child sent it", "parent done")
    paused = await runtime.run(_request())
    before = (await runtime.status(paused.run_id, "t")).deadline
    await asyncio.sleep(0.05)

    await runtime.resume(paused.run_id, "t", {"decision": "approved"})
    final = await _settled(runtime, paused.run_id)

    assert final.status == RunStatus.COMPLETED
    assert before is not None and final.deadline is not None and final.deadline > before


async def test_a_conversation_turn_shows_and_decides_the_childs_approval() -> None:
    from algen_agent_runtime.conversations import (
        ConversationApprovalDecision,
        ConversationHandlerRegistry,
        ConversationService,
        MessageStatus,
        RuntimeConversationHandler,
    )
    from algen_agent_runtime.conversations.stores import (
        InMemoryConversationEventBus,
        InMemoryConversationStore,
    )

    runtime, sent = _runtime(DELEGATION, SEND, "child sent it", "parent done")
    handlers = ConversationHandlerRegistry()
    handlers.register(RuntimeConversationHandler(runtime))
    service = ConversationService(
        InMemoryConversationStore(), InMemoryConversationEventBus(), handlers
    )
    conversation = await service.create(tenant_id="t", user_id="alice", agent="test-agent")
    await service.submit(conversation.id, "t", "alice", "mail bob")

    async def last(status: MessageStatus) -> Any:
        for _ in range(400):
            [*_, message] = await service.messages(conversation.id, "t")
            if message.status == status:
                return message
            await asyncio.sleep(0.005)
        raise AssertionError(f"turn never reached {status}")

    waiting = await last(MessageStatus.AWAITING_APPROVAL)
    pending = waiting.pending
    [child] = _children(runtime, pending.run_id)
    assert (pending.tool, pending.child_run_id) == ("mail.send", child.id)
    assert pending.approval_id == (child.pause_payload or {})["approval_id"]

    await service.respond(
        conversation.id,
        pending.turn_id,
        "t",
        "alice",
        ConversationApprovalDecision(decision="approved"),
    )
    done = await last(MessageStatus.COMPLETED)

    assert done.text_content == "parent done"
    assert sent == ["bob"]
    approval = await runtime.approvals.get(str(pending.approval_id), "t")
    assert approval is not None and approval.decided_by == "alice"
