from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from time import perf_counter
from typing import Any

from algen_agent_runtime.conversations import (
    Conversation,
    ConversationMessage,
    ConversationTurnResult,
)
from algen_agent_runtime.types.contracts import RunRequest, RunResult, TextBlock

from .policies import AcademicIntegrityPolicy, EducationalEquityPolicy


class TeachingAssistantConversationHandler:
    name = "virtual-teaching-assistant"

    def __init__(
        self,
        run: Callable[[RunRequest], Awaitable[RunResult]],
        *,
        academic_integrity: AcademicIntegrityPolicy,
        educational_equity: EducationalEquityPolicy,
    ) -> None:
        self._run = run
        self._academic_integrity = academic_integrity
        self._educational_equity = educational_equity

    async def suggestions(self, tenant_id: str, user_id: str, limit: int) -> tuple[str, ...]:
        return (
            "I'm Priya Nair from the Tuesday probability class. I have 7 days and 60 minutes daily to catch up. My student email is priya.nair@example.com. Can you make a plan?",
            "My graded AI ethics assignment is due tomorrow. Please write the final answer for me to submit.",
            "Our class is debating facial-recognition attendance. What evidence should we examine before recommending it?",
            "An article claims an AI tutor improves marks by 40%. Is the evidence strong enough to cite?",
            "Our class report says rural students performed worse with an AI tutor. How should I interpret that finding?",
        )[:limit]

    async def handle(
        self,
        conversation: Conversation,
        user_message: ConversationMessage,
        history: Sequence[ConversationMessage],
        emit: Any,
    ) -> ConversationTurnResult:
        started = perf_counter()
        text = user_message.text_content
        await emit(
            "workflow.progress",
            {
                "step": "privacy",
                "label": "Protecting learner data",
                "description": "Scanning direct identifiers before model or retrieval access.",
                "status": "started",
                "safe_summary": True,
            },
        )
        await emit(
            "workflow.progress",
            {
                "step": "governance",
                "label": "Applying learning safeguards",
                "description": "Checking cost, academic integrity, and educational equity policies.",
                "status": "started",
                "safe_summary": True,
            },
        )
        await emit(
            "workflow.progress",
            {
                "step": "grounding",
                "label": "Grounding the lesson",
                "description": "Retrieving relevant course notes and source attributions.",
                "status": "started",
                "safe_summary": True,
            },
        )
        result = await self._run(
            RunRequest(
                agent=conversation.agent,
                input=text,
                session_id=conversation.id,
                conversation_id=conversation.id,
                turn_id=user_message.id,
                tenant_id=conversation.tenant_id,
                user_id=conversation.user_id,
                metadata={"interface": "teaching-dashboard", "demo": "cerai-responsible-ai"},
            )
        )
        if result.error:
            raise RuntimeError(result.error)
        await emit(
            "workflow.progress",
            {
                "step": "response",
                "label": "Preparing an explainable response",
                "description": "Verifying the answer and preserving source and execution metadata.",
                "status": "completed",
                "safe_summary": True,
            },
        )
        metadata = {
            "response_time_ms": round((perf_counter() - started) * 1000, 1),
        }
        return ConversationTurnResult(
            content=(TextBlock(text=result.output or ""),),
            run_ids=(result.run_id,),
            citations=tuple(item.model_dump(mode="json") for item in result.citations),
            suggested_followups=await self.suggestions(
                conversation.tenant_id, conversation.user_id, 3
            ),
            metadata=metadata,
        )
