from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionType(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    CLARIFY = "clarify"
    APPROVE = "approve"
    COMPLETE = "complete"
    FAIL = "fail"


class PlannedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    type: ActionType
    description: str
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    model_profile: str | None = None
    depends_on: tuple[str, ...] = ()
    condition: str | None = None
    requires_approval: bool = False

    @model_validator(mode="after")
    def validate_action(self) -> PlannedAction:
        if self.type == ActionType.TOOL and not self.tool_name:
            raise ValueError("tool action requires tool_name")
        if self.type != ActionType.TOOL and self.tool_name:
            raise ValueError("tool_name is only valid for tool actions")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    actions: tuple[PlannedAction, ...]
    decision_summary: str

    @model_validator(mode="after")
    def validate_graph(self) -> Plan:
        ids = [action.id for action in self.actions]
        if len(ids) != len(set(ids)):
            raise ValueError("plan action ids must be unique")
        known: set[str] = set()
        for action in self.actions:
            if not set(action.depends_on).issubset(known):
                raise ValueError(f"action {action.id!r} has missing or forward dependencies")
            known.add(action.id)
        return self
