"""Credential-free demonstration of recoverable, governed tool-result paging."""

from __future__ import annotations

import asyncio
import json

from algen_agent_runtime.context.artifacts import CONTEXT_READ_TOOL, ContextArtifactService
from algen_agent_runtime.persistence.memory import InMemoryArtifactStore
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import ToolContext
from algen_agent_runtime.tools.executor import ToolExecutor
from algen_agent_runtime.tools.registry import ToolRegistry
from algen_agent_runtime.types.contracts import ContextPolicy, utc_now


async def main() -> None:
    service = ContextArtifactService(InMemoryArtifactStore())
    context = ToolContext(
        tenant_id="example",
        user_id="operator",
        run_id="paging-demo",
        step_id="fetch",
        permissions=frozenset({"context.read"}),
        idempotency_key="paging-demo:fetch",
    )
    # In a real runtime run, ToolExecutor applies validation and output policies first.
    processed_output = json.dumps(
        {"rows": [{"id": index, "value": index * 2} for index in range(1000)]}
    )
    reference, artifact_id = await service.offload(
        processed_output, context, ContextPolicy(offload_tool_results=True), started_at=utc_now()
    )
    assert artifact_id is not None
    registry = ToolRegistry()
    registry.register(service.read_tool())
    executor = ToolExecutor(registry, CompositePolicyEngine())
    parts: list[str] = []
    offset = 0
    while True:
        result = await executor.execute(
            CONTEXT_READ_TOOL,
            {"artifact_id": artifact_id, "offset": offset, "limit": 1024},
            context.model_copy(
                update={
                    "step_id": f"read-{offset}",
                    "idempotency_key": f"paging-demo:read-{offset}",
                }
            ),
        )
        page = result.value
        parts.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(parts) == processed_output
    print(
        f"Stored {len(processed_output.encode())} bytes; reference is {len(reference.encode())} bytes."
    )
    print(f"Recovered all 1,000 rows through {len(parts)} bounded, governed reads.")


if __name__ == "__main__":
    asyncio.run(main())
