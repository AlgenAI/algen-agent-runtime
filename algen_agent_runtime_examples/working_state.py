"""Keep structured task progress in a run checkpoint without an extra model call."""

from __future__ import annotations

import asyncio

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ApprovalPolicy,
    ContextPolicy,
    ModelProfile,
    RunRequest,
    RunStatus,
)


async def main() -> None:
    container = build_container(AppSettings())
    runtime = container.runtime
    runtime.router.register_provider(
        MockModelProvider(
            [
                tool_call_response(
                    "core.working_state_update",
                    {
                        "expected_revision": 0,
                        "state": {
                            "goal": "Complete the comparison report",
                            "constraints": ["Use verified source data"],
                            "decisions": ["Compare the two proposed options"],
                            "completed_work": ["Collected source data"],
                            "open_tasks": ["Check totals", "Write the recommendation"],
                        },
                    },
                ),
                "Source data is collected. Next: check totals and write the recommendation.",
            ]
        ),
        registration_id="working-state-demo",
    )
    runtime.agents.register(
        AgentDefinition(
            name="working-state-demo",
            version="1.0.0",
            description="Offline structured working-state example",
            system_instructions="Keep concise task progress using the working-state tool.",
            default_model=ModelProfile(
                name="demo", provider="working-state-demo", model="deterministic"
            ),
            enabled_tools=frozenset({"core.working_state_update"}),
            tool_permissions=frozenset({"context.write"}),
            context_policy=ContextPolicy(working_state_enabled=True),
            # This demo enables only the run-local working-state tool.
            approval_policy=ApprovalPolicy(require_for_side_effects=False),
        )
    )
    try:
        result = await runtime.run(
            RunRequest(
                agent="working-state-demo",
                input="Record the report progress.",
                tenant_id="example",
                user_id="operator",
            )
        )
        if result.status != RunStatus.COMPLETED:
            raise RuntimeError(result.error or result.status.value)
        checkpoint = await runtime.status(result.run_id, "example")
        assert checkpoint.working_state is not None
        print(f"Working-state revision: {checkpoint.working_state.revision}")
        print(f"Open tasks: {', '.join(checkpoint.working_state.state.open_tasks)}")
        print(
            f"Model calls: {result.execution_summary.model_calls}; tool calls: {result.execution_summary.tool_calls}"
        )
        print(result.output)
    finally:
        container.close()


if __name__ == "__main__":
    asyncio.run(main())
