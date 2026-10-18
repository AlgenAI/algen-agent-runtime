"""Remember a preference and recall it in another run using the ephemeral demo store."""

from __future__ import annotations

import asyncio

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.memory.contracts import MemoryScope
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    AgentDefinition,
    ApprovalPolicy,
    LearnedMemoryPolicy,
    ModelProfile,
    RunRequest,
    RunStatus,
)


async def main() -> None:
    container = build_container(AppSettings())
    runtime = container.runtime
    provider = MockModelProvider(
        [
            tool_call_response(
                "core.memory_write",
                {
                    "key": "report-format",
                    "expected_revision": 0,
                    "kind": "preference",
                    "content": "Use concise tables",
                    "evidence": "The user requested concise comparison tables",
                    "retention_seconds": 86400,
                },
            ),
            "Saved your reporting preference.",
            tool_call_response("core.memory_recall", {"key": "report-format"}),
            "I will use concise comparison tables.",
        ]
    )
    runtime.router.register_provider(provider, registration_id="memory-demo")
    runtime.agents.register(
        AgentDefinition(
            name="memory-demo",
            version="1.0.0",
            description="Offline learned-memory example",
            system_instructions="Remember explicitly requested preferences and recall them when relevant.",
            default_model=ModelProfile(name="demo", provider="memory-demo", model="deterministic"),
            enabled_tools=frozenset({"core.memory_write", "core.memory_recall"}),
            tool_permissions=frozenset({"memory.write", "memory.read"}),
            learned_memory_policy=LearnedMemoryPolicy(enabled=True),
            # Only the scoped memory write is enabled in this demo.
            approval_policy=ApprovalPolicy(require_for_side_effects=False),
        )
    )
    try:
        for text in [
            "Remember that I prefer concise comparison tables.",
            "How should you format my next report?",
        ]:
            result = await runtime.run(
                RunRequest(agent="memory-demo", input=text, tenant_id="example", user_id="operator")
            )
            if result.status != RunStatus.COMPLETED:
                raise RuntimeError(result.error or result.status.value)
            print(result.output)
        assert runtime.learned_memory is not None
        record = await runtime.learned_memory.store.get(
            MemoryScope(tenant_id="example", user_id="operator", agent_id="memory-demo"),
            "report-format",
        )
        assert record is not None
        print(
            f"Recalled revision {record.revision} across two runs; provenance and expiry retained."
        )
        print("Demo storage is ephemeral. Configure learned_memory_store: postgres for durability.")
    finally:
        await container.aclose()


if __name__ == "__main__":
    asyncio.run(main())
