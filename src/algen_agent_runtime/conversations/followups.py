from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.conversations.contracts import ConversationMessage
from algen_agent_runtime.types.contracts import RequestOverrides, RunRequest


class FollowupSuggestions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suggestions: tuple[str, ...] = Field(max_length=10)


class FollowupSuggestionProvider(Protocol):
    async def suggest(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        history: Sequence[ConversationMessage],
        user_text: str,
        assistant_text: str,
    ) -> tuple[str, ...]: ...


class ModelFollowupSuggestionProvider:
    """Generate bounded next-turn prompts through a separately governed runtime agent."""

    def __init__(
        self,
        runtime: Any,
        *,
        agent: str,
        max_suggestions: int = 3,
        history_messages: int = 8,
        maximum_length: int = 240,
    ) -> None:
        self._runtime = runtime
        self._agent = agent
        self._max_suggestions = max_suggestions
        self._history_messages = history_messages
        self._maximum_length = maximum_length

    async def suggest(
        self,
        *,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        history: Sequence[ConversationMessage],
        user_text: str,
        assistant_text: str,
    ) -> tuple[str, ...]:
        transcript = [
            {"role": item.role.value, "text": item.text_content[:2_000]}
            for item in history[-self._history_messages :]
            if item.text_content
        ]
        response = await self._runtime.run(
            RunRequest(
                agent=self._agent,
                input=json.dumps(
                    {
                        "recent_conversation": transcript,
                        "current_user_question": user_text[:4_000],
                        "current_assistant_response": assistant_text[:8_000],
                        "maximum_suggestions": self._max_suggestions,
                        "maximum_characters_each": self._maximum_length,
                    },
                    ensure_ascii=False,
                ),
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=conversation_id,
                conversation_id=conversation_id,
                metadata={"workflow": "conversation-followups", "agent_role": "followups"},
                overrides=RequestOverrides(response_schema=FollowupSuggestions.model_json_schema()),
            )
        )
        if response.error or not response.output:
            return ()
        parsed = FollowupSuggestions.model_validate_json(response.output)
        cleaned: list[str] = []
        seen: set[str] = set()
        for suggestion in parsed.suggestions:
            value = " ".join(suggestion.split()).strip()[: self._maximum_length]
            normalized = value.casefold()
            if not value or normalized in seen:
                continue
            if any(
                term in normalized
                for term in ("system prompt", "hidden instruction", "chain of thought")
            ):
                continue
            seen.add(normalized)
            cleaned.append(value)
            if len(cleaned) >= self._max_suggestions:
                break
        return tuple(cleaned)
