"""The tool-schema hints only the guardrails read never reach a model provider."""

from __future__ import annotations

from typing import Any

from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.policies.engine import without_policy_hints


def test_policy_hints_are_removed_and_the_rest_kept() -> None:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "chat_id": {"type": "string", "format": "identifier", "description": "Chat"},
            "order": {"type": "string", "x-pii": False},
            "to": {"type": "string", "format": "email"},
            "ids": {"type": "array", "items": {"type": "string", "format": "identifier"}},
        },
        "required": ["chat_id"],
        "additionalProperties": False,
    }

    sent = without_policy_hints(schema)

    assert sent["properties"]["chat_id"] == {"type": "string", "description": "Chat"}
    assert sent["properties"]["order"] == {"type": "string"}
    assert sent["properties"]["to"]["format"] == "email"  # a real format is kept
    assert sent["properties"]["ids"]["items"] == {"type": "string"}
    assert sent["required"] == ["chat_id"] and sent["additionalProperties"] is False
    assert schema["properties"]["chat_id"]["format"] == "identifier"  # the definition is unchanged


async def test_a_run_sends_tool_schemas_without_policy_hints() -> None:
    from algen_agent_runtime.config.settings import AppSettings
    from algen_agent_runtime.orchestration.container import build_container
    from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolDefinition
    from algen_agent_runtime.types.contracts import RunRequest, RunStatus

    async def echo(arguments: dict[str, Any], context: Any) -> Any:
        return arguments

    tool = Tool(
        ToolDefinition(
            name="chat.read",
            version="1.0.0",
            description="Read a chat",
            input_schema={
                "type": "object",
                "properties": {"chat_id": {"type": "string", "format": "identifier"}},
                "required": ["chat_id"],
                "additionalProperties": False,
            },
            output_schema={},
            side_effect=SideEffect.READ,
        ),
        echo,
    )
    settings = AppSettings.model_validate(
        {
            "providers": {"mock": {"type": "mock", "default_model": "deterministic"}},
            "agents": [
                {
                    "name": "reader",
                    "version": "1.0.0",
                    "description": "Reads",
                    "system_instructions": "Read.",
                    "default_model": {"name": "d", "provider": "mock", "model": "deterministic"},
                    "enabled_tools": ["chat.read"],
                }
            ],
        }
    )
    container = build_container(settings, additional_tools=(tool,))
    provider = container.runtime.router.provider("mock")
    assert isinstance(provider, MockModelProvider)
    provider.queue("done")
    result = await container.runtime.run(
        RunRequest(agent="reader", input="hi", tenant_id="t", user_id="u")
    )

    assert result.status is RunStatus.COMPLETED, result.error
    [request] = provider.requests
    [spec] = request.tools
    assert "format" not in spec.input_schema["properties"]["chat_id"]
