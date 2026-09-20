from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from examples.case_study_teaching_assistant.application.tools import register_teaching_tools


def _context(permission: str) -> ToolContext:
    return ToolContext(
        run_id="run",
        step_id="step",
        tenant_id="tenant",
        user_id="learner",
        permissions=frozenset({permission}),
        idempotency_key=f"run:{permission}",
    )


async def test_teaching_tools_are_registered_and_contract_valid() -> None:
    registry = ToolRegistry()
    register_teaching_tools(registry)
    executor = ToolExecutor(registry, CompositePolicyEngine())

    plan = await executor.execute(
        "teaching.build_study_plan",
        {"topic": "probability", "days": 10, "minutes_per_day": 90},
        _context("learning.plan.read"),
    )
    case = await executor.execute(
        "teaching.get_case_evidence",
        {"case_id": "facial_recognition_attendance"},
        _context("learning.case.read"),
    )

    assert plan.value["daily_minutes"] == 90
    assert len(plan.value["phases"]) == 3
    assert case.value["missing_evidence"]
