import pytest
from pydantic import ValidationError

from algen_agent_runtime.planning.contracts import ActionType, Plan, PlannedAction


def test_plan_rejects_forward_dependency() -> None:
    with pytest.raises(ValidationError):
        Plan(
            decision_summary="bad",
            actions=(
                PlannedAction(id="a", type=ActionType.MODEL, description="x", depends_on=("b",)),
            ),
        )


def test_tool_action_requires_name() -> None:
    with pytest.raises(ValidationError):
        PlannedAction(id="a", type=ActionType.TOOL, description="x")
