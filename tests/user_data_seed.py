"""Seed one user's data in any set of stores, for the user data export and erasure tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from algen_agent_runtime.conversations.contracts import (
    Conversation,
    ConversationEvent,
    ConversationMessage,
)
from algen_agent_runtime.conversations.feedback import ConversationFeedback, FeedbackRating
from algen_agent_runtime.events.contracts import RunEvent
from algen_agent_runtime.memory.contracts import LearnedMemory, MemoryMutation, MemoryScope
from algen_agent_runtime.tools.contracts import ToolContext, ToolResult
from algen_agent_runtime.types.contracts import (
    Artifact,
    Message,
    Role,
    RunAttachment,
    RunRequest,
    RunState,
    RunStatus,
    utc_now,
)
from algen_agent_runtime.workflows.contracts import WorkflowExecutionState, WorkflowStatus


@dataclass
class Seeded:
    run_ids: list[str]
    session_id: str
    conversation_id: str
    artifact_ids: list[str]
    workflow_id: str


async def seed_user(
    stores: dict[str, Any],
    tenant: str,
    user: str,
    *,
    status: RunStatus = RunStatus.COMPLETED,
) -> Seeded:
    """Two runs of ``user`` (one with an uploaded attachment) with an event, an approval and a
    tool execution each, an artifact the run wrote, session memory, a learned memory record, a
    conversation with two messages, an event and feedback, and a finished workflow run."""
    marker = uuid4().hex[:8]
    session = f"session-{marker}"
    upload = Artifact(
        tenant_id=tenant, name="upload.txt", media_type="text/plain", data=b"user file"
    )
    await stores["artifacts"].put(upload)
    run_ids: list[str] = []
    artifact_ids = [upload.id]
    for index in range(2):
        attachments = (RunAttachment(artifact_id=upload.id),) if index == 0 else ()
        state = RunState(
            id=f"run-{marker}-{index}",
            request=RunRequest(
                agent="test-agent",
                input=f"question {index} from {user}",
                tenant_id=tenant,
                user_id=user,
                session_id=session,
                attachments=attachments,
            ),
            session_id=session,
            status=status,
        )
        await stores["runs"].create(state)
        run_ids.append(state.id)
        await stores["events"].publish(
            RunEvent(
                type="run.started",
                run_id=state.id,
                tenant_id=tenant,
                session_id=session,
                correlation_id=state.id,
                sequence=1,
            )
        )
        await stores["approvals"].create(
            state.id, "step-1", tenant, "send_mail", {"to": "x"}, "high", 3600
        )
        context = ToolContext(
            run_id=state.id,
            step_id="step-1",
            tenant_id=tenant,
            user_id=user,
            permissions=frozenset(),
            idempotency_key=f"{state.id}:step-1:tool",
        )
        await stores["tool_executions"].begin(context, "lookup")
        await stores["tool_executions"].complete(context.idempotency_key, ToolResult(value=1))
        written = Artifact(
            tenant_id=tenant,
            run_id=state.id,
            name="answer.txt",
            media_type="text/plain",
            data=b"run output",
        )
        await stores["artifacts"].put(written)
        artifact_ids.append(written.id)
    memory = stores["memory"]
    assert await memory.claim_session(tenant, session, user)
    await memory.append(tenant, session, [Message.text(Role.USER, f"I am {user}")])
    learned = stores.get("learned_memory")
    if learned is not None:
        now = utc_now()
        await learned.apply(
            MemoryMutation(
                scope=MemoryScope(tenant_id=tenant, user_id=user, agent_id="test-agent"),
                operation_id=f"op-{marker}",
                expected_revision=0,
                record=LearnedMemory(
                    key="likes.tea",
                    revision=1,
                    kind="preference",
                    content="likes tea",
                    evidence="said so",
                    source_run_id=run_ids[0],
                    source_step_id="step-1",
                    source_agent_version="1.0.0",
                    updated_at=now,
                    expires_at=now + timedelta(days=1),
                ),
            )
        )
    conversation = Conversation(tenant_id=tenant, user_id=user, agent="test-agent")
    conversations = stores.get("conversations")
    if conversations is not None:
        await conversations.create(conversation)
        assert await memory.claim_session(tenant, conversation.id, user)
        await memory.append(tenant, conversation.id, [Message.text(Role.TOOL, "tool result")])
        question = ConversationMessage.text(
            conversation_id=conversation.id, tenant_id=tenant, role=Role.USER, text="hi"
        )
        answer = ConversationMessage.text(
            conversation_id=conversation.id,
            tenant_id=tenant,
            role=Role.ASSISTANT,
            text="hello",
            run_ids=(run_ids[1],),
        )
        await conversations.append_message(question)
        await conversations.append_message(answer)
        await stores["conversation_events"].publish(
            ConversationEvent(
                type="message.created",
                conversation_id=conversation.id,
                tenant_id=tenant,
                message_id=question.id,
                sequence=1,
            )
        )
        await stores["conversation_feedback"].put(
            ConversationFeedback(
                tenant_id=tenant,
                user_id=user,
                conversation_id=conversation.id,
                message_id=answer.id,
                rating=FeedbackRating.UP,
            )
        )
    workflow = WorkflowExecutionState(
        id=f"wf-{marker}",
        manifest_name="flow",
        manifest_version="1",
        tenant_id=tenant,
        user_id=user,
        status=WorkflowStatus.COMPLETED,
    )
    workflows = stores.get("workflow_checkpoints")
    if workflows is not None:
        await workflows.create(workflow)
    return Seeded(run_ids, session, conversation.id, artifact_ids, workflow.id)


# What seed_user writes, per kind, with every store present.
SEEDED_COUNTS = {
    "conversation_feedback": 1,
    "conversation_events": 1,
    "conversation_messages": 2,
    "conversations": 1,
    "artifacts": 3,
    "tool_executions": 2,
    "approvals": 2,
    "run_events": 2,
    "runs": 2,
    "workflow_runs": 1,
    "session_memory": 2,
    "session_owners": 2,
    "learned_memory": 1,
    "mcp_connections": 0,
}
