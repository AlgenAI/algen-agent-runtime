"""Run history compaction offline with a deterministic summary and answer."""

from __future__ import annotations

import asyncio

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    Budget,
    ContextPolicy,
    Message,
    ModelProfile,
    Role,
    RunRequest,
)


async def main() -> None:
    container = build_container(AppSettings())
    runtime = container.runtime
    runtime.router.register_provider(
        MockModelProvider(
            [
                "The previous report was reviewed. Continue with the user's current question.",
                "The current answer uses the retained goal and recent context.",
            ]
        ),
        registration_id="compaction-demo",
    )
    runtime.agents.register(
        AgentDefinition(
            name="history-demo",
            version="1.0.0",
            description="Offline compaction example",
            system_instructions="Use the retained goal and available evidence.",
            default_model=ModelProfile(
                name="demo",
                provider="compaction-demo",
                model="deterministic",
                context_window_tokens=2048,
            ),
            budget=Budget(max_output_tokens=256),
            enabled_tools=frozenset({"core.context_read"}),
            tool_permissions=frozenset({"context.read"}),
            context_policy=ContextPolicy(
                compact_history=True,
                archive_compacted_history=True,
                compaction_trigger_ratio=0.8,
                compaction_keep_recent_groups=2,
                compaction_max_output_tokens=128,
            ),
        )
    )
    try:
        await runtime.memory.append(
            "example",
            "session",
            [
                Message.text(Role.USER, "Previous report details. " * 200),
                Message.text(Role.ASSISTANT, "The report was reviewed."),
            ],
        )
        result = await runtime.run(
            RunRequest(
                agent="history-demo",
                input="Continue with the next question.",
                session_id="session",
                tenant_id="example",
                user_id="operator",
            )
        )
        if result.error:
            raise RuntimeError(result.error)
        state = await runtime.status(result.run_id, "example")
        assert state.context_compaction is not None
        assert state.context_history is not None
        print(
            f"Retained {len(state.context_history)} tail messages at offset "
            f"{state.context_history_offset}; source archives: {len(result.artifacts)}."
        )
        print(f"Model calls including summarization: {result.execution_summary.model_calls}.")
        print(result.output)
    finally:
        container.close()


if __name__ == "__main__":
    asyncio.run(main())
