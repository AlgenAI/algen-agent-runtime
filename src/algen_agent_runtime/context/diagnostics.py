from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from algen_agent_runtime.context.packing import (
    CONTEXT_SUMMARY_NAME,
    LOADED_SKILLS_NAME,
    SKILL_CATALOG_NAME,
    WORKING_STATE_NAME,
    estimate_message,
)
from algen_agent_runtime.types.contracts import Role, RunState

CompactionReason = Literal[
    "disabled",
    "history_uninitialized",
    "pending_tools",
    "below_trigger",
    "recent_groups_only",
    "incomplete_tool_group",
    "input_limit",
    "no_source",
    "ready",
]


class CompactionDiagnostic(BaseModel):
    """Content-free planning evidence; ready does not mean a summary was committed."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    reason: CompactionReason
    estimated_context_tokens: int = Field(ge=0)
    trigger_tokens: int = Field(ge=1)
    max_input_tokens: int = Field(ge=1)
    planned_message_count: int = Field(default=0, ge=0)
    history_offset: int = Field(ge=0)


class ContextSnapshot(BaseModel):
    """Estimates of the post-builder, post-policy view, before router packing."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    estimated_tokens: int = Field(ge=0)
    retained_history_messages: int | None = Field(ge=0)
    archived_history_messages: int = Field(ge=0)
    summary_tokens: int = Field(ge=0)
    working_state_tokens: int = Field(ge=0)
    skill_catalog_tokens: int = Field(ge=0)
    loaded_skills_tokens: int = Field(ge=0)


def context_snapshot(state: RunState) -> ContextSnapshot:
    costs = [(message, estimate_message(message)) for message in state.messages]

    def named_tokens(name: str) -> int:
        return sum(
            cost
            for message, cost in costs
            if message.role == Role.ASSISTANT and message.name == name
        )

    return ContextSnapshot(
        estimated_tokens=sum(cost for _, cost in costs),
        retained_history_messages=len(state.context_history)
        if state.context_history is not None
        else None,
        archived_history_messages=state.context_history_offset,
        summary_tokens=named_tokens(CONTEXT_SUMMARY_NAME),
        working_state_tokens=named_tokens(WORKING_STATE_NAME),
        skill_catalog_tokens=named_tokens(SKILL_CATALOG_NAME),
        loaded_skills_tokens=named_tokens(LOADED_SKILLS_NAME),
    )
