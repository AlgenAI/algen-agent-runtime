from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.frameworks import (
    FrameworkRunRequest,
    GovernedToolBridge,
    OpenAIAgentsAdapter,
)
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition

ACCOUNT_SCHEMA = {
    "type": "object",
    "properties": {"incident": {"type": "string"}},
    "required": ["incident"],
    "additionalProperties": False,
}


def runtime_tools(log: list[str]) -> tuple[Tool, ...]:
    """Runtime tools the SDK agent may call: one read, one that needs a person's approval."""

    async def lookup(arguments: dict[str, Any], context: ToolContext) -> Any:
        log.append(f"incidents.lookup {arguments['incident']} for {context.tenant_id}")
        return {"incident": arguments["incident"], "severity": "high", "service": "payments"}

    async def close(arguments: dict[str, Any], context: ToolContext) -> Any:
        log.append(f"incidents.close {arguments['incident']}")  # never reached through the bridge
        return {"closed": True}

    def definition(name: str, description: str, *, approval: bool) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            version="1.0.0",
            description=description,
            input_schema=ACCOUNT_SCHEMA,
            output_schema={},
            required_permissions=frozenset({"incidents.read"}),
            side_effect=SideEffect.WRITE if approval else SideEffect.READ,
            requires_approval=approval,
        )

    return (
        Tool(definition("incidents.lookup", "Look up an incident", approval=False), lookup),
        Tool(definition("incidents.close", "Close an incident", approval=True), close),
    )


async def run_openai_agents_demonstration() -> dict[str, Any]:
    """Demonstrate OpenAI Agents SDK application registration and invoke/stream.

    Note: Runtime normalizes the outer invocation result/events and identity metadata.
    Internal SDK operations (model calls, function tools, handoffs, guardrails) remain
    under native OpenAI Agents SDK control.
    """
    from agents import Agent, RunConfig, Runner, function_tool
    from agents.testing import ScriptedModel, assistant_message, function_call

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

        # 3. Hand Runtime tools to the SDK agent. Calls to them go through the Runtime's tool
        # executor (permissions, validation, policy, ledger); the SDK's own tools do not.
        governed: list[str] = []
        for tool in runtime_tools(governed):
            container.tools.register(tool)
        bridge = GovernedToolBridge(
            container.runtime.tool_executor, container.tools, req, permissions={"incidents.read"}
        )
        native: list[str] = []

        @function_tool
        def read_runbook(incident: str) -> str:
            """A tool the SDK runs on its own: traced by the SDK, not governed by the Runtime."""
            native.append(f"read_runbook {incident}")
            return "Restart the payments worker."

        tool_agent = Agent(
            name="incident-responder",
            model=ScriptedModel(
                [
                    [
                        function_call("incidents_lookup", {"incident": "INC-1024"}, call_id="c1"),
                        function_call("incidents_close", {"incident": "INC-1024"}, call_id="c2"),
                        function_call("read_runbook", {"incident": "INC-1024"}, call_id="c3"),
                    ],
                    [assistant_message("Looked up INC-1024; closing it needs a person.")],
                ]
            ),
            tools=[
                *bridge.openai_agents_tools("incidents.lookup", "incidents.close"),
                read_runbook,
            ],
        )
        tool_run = await Runner.run(
            tool_agent, req.input, run_config=RunConfig(tracing_disabled=True)
        )

        return {
            "framework_id": framework.framework_id,
            "status": invoke_result.status.value,
            "output": invoke_result.output,
            "stream_events": stream_events,
            "governed_calls": governed,
            "native_calls": native,
            "tool_run_output": tool_run.final_output,
        }
    finally:
        await container.aclose()


def main() -> None:
    result = asyncio.run(run_openai_agents_demonstration())
    print(f"Framework: {result['framework_id']}")
    print(f"Status: {result['status']}")
    print(f"Output: {result['output']}")
    print(f"Stream Events: {result['stream_events']}")
    print(f"Governed by the Runtime: {result['governed_calls']}")
    print(f"Run by the SDK alone: {result['native_calls']}")
    print(f"Agent: {result['tool_run_output']}")


if __name__ == "__main__":
    main()
