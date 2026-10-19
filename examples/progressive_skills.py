"""Load selected procedural guidance from a small catalog using an offline model."""

from __future__ import annotations

import asyncio

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.context.packing import LOADED_SKILLS_NAME
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ContextPolicy,
    ModelProfile,
    RunRequest,
    RunStatus,
    SkillDefinition,
)


async def main() -> None:
    container = build_container(AppSettings())
    runtime = container.runtime
    provider = MockModelProvider(
        [
            tool_call_response("core.skills_load", {"names": ["reporting"]}),
            "Check the evidence, compare totals, and list unresolved discrepancies.",
        ]
    )
    runtime.router.register_provider(provider, registration_id="skills-demo")
    runtime.agents.register(
        AgentDefinition(
            name="skills-demo",
            version="1.0.0",
            description="Offline progressive skill loading",
            system_instructions="Load relevant skill guidance before preparing the report.",
            default_model=ModelProfile(name="demo", provider="skills-demo", model="deterministic"),
            enabled_tools=frozenset({"core.skills_load"}),
            tool_permissions=frozenset({"skills.read"}),
            context_policy=ContextPolicy(skills_enabled=True),
            skills=(
                SkillDefinition(
                    name="reporting",
                    version="1.0.0",
                    description="Prepare a report from verified evidence.",
                    instructions="Check source completeness, compare totals, and list unresolved discrepancies.",
                ),
                SkillDefinition(
                    name="editing",
                    version="1.0.0",
                    description="Edit a completed report for clarity.",
                    instructions="Preserve the evidence and conclusions while simplifying prose.",
                ),
            ),
        )
    )
    try:
        result = await runtime.run(
            RunRequest(
                agent="skills-demo",
                input="Help me prepare the comparison report.",
                tenant_id="example",
                user_id="operator",
            )
        )
        if result.status != RunStatus.COMPLETED:
            raise RuntimeError(result.error or result.status.value)
        assert not any(m.name == LOADED_SKILLS_NAME for m in provider.requests[0].messages)
        checkpoint = await runtime.status(result.run_id, "example")
        assert [s.name for s in checkpoint.loaded_skills.skills] == ["reporting"]
        print("Initial context: descriptions only.")
        print("Loaded guidance: reporting@1.0.0; editing remains unloaded.")
        print(
            f"Model calls: {result.execution_summary.model_calls}; tool calls: {result.execution_summary.tool_calls}"
        )
        print(result.output)
    finally:
        container.close()


if __name__ == "__main__":
    asyncio.run(main())
