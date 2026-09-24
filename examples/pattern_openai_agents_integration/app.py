from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.frameworks import FrameworkRunRequest, OpenAIAgentsAdapter
from algen_agent_runtime.orchestration.container import build_container


async def run_openai_agents_demonstration() -> dict[str, Any]:
    """Demonstrate OpenAI Agents SDK application registration and invoke/stream.

    Note: Runtime normalizes the outer invocation result/events and identity metadata.
    Internal SDK operations (model calls, function tools, handoffs, guardrails) remain
    under native OpenAI Agents SDK control.
    """
    from agents import Agent
    from agents.testing import ScriptedModel, assistant_message

    # Construct application-owned Agent using credential-free ScriptedModel
    scripted_model = ScriptedModel(
        [
            [assistant_message("Log analysis completed by OpenAI Agents SDK.")],
            [assistant_message("Second turn response.")],
        ]
    )
    agent = Agent(name="incident-analyst", model=scripted_model)
    adapter = OpenAIAgentsAdapter(agent)

    # Load settings and register adapter into container
    config_path = Path(__file__).with_name("agent.yaml")
    settings = load_settings((config_path,))
    container = build_container(settings, framework_adapters=(adapter,))
    await container.astart()

    try:
        framework = container.frameworks.get("openai_agents")

        # 1. Invoke through container framework registry
        req = FrameworkRunRequest(
            input="Analyze security incident INC-1024",
            run_id="run-incident-1024",
            tenant_id="security-ops",
            user_id="sec-analyst-1",
            session_id="incident-room-42",
        )
        invoke_result = await framework.invoke(req)

        # 2. Stream through container framework registry
        stream_events = []
        async for event in framework.stream(req):
            stream_events.append(event.type.value)

        return {
            "framework_id": framework.framework_id,
            "status": invoke_result.status.value,
            "output": invoke_result.output,
            "stream_events": stream_events,
        }
    finally:
        await container.aclose()


def main() -> None:
    result = asyncio.run(run_openai_agents_demonstration())
    print(f"Framework: {result['framework_id']}")
    print(f"Status: {result['status']}")
    print(f"Output: {result['output']}")
    print(f"Stream Events: {result['stream_events']}")


if __name__ == "__main__":
    main()
