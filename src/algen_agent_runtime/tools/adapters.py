from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import httpx

from algen_agent_runtime.security.network import validate_outbound_url
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)


class MCPClient(Protocol):
    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


def mcp_tool(definition: ToolDefinition, remote_name: str, client: MCPClient) -> Tool:
    async def execute(arguments: dict[str, Any], context: ToolContext) -> Any:
        return await client.call_tool(remote_name, arguments)

    return Tool(definition, execute)


def remote_tool(
    definition: ToolDefinition,
    endpoint: str,
    *,
    allowed_hosts: tuple[str, ...],
    allow_private_networks: bool = False,
    client: httpx.AsyncClient | None = None,
) -> Tool:
    http_client = client or httpx.AsyncClient(follow_redirects=False)

    async def execute(arguments: dict[str, Any], context: ToolContext) -> Any:
        await validate_outbound_url(endpoint, allowed_hosts, allow_private_networks)
        response = await http_client.post(
            endpoint,
            json={
                "arguments": arguments,
                "context": {
                    "run_id": context.run_id,
                    "step_id": context.step_id,
                    "tenant_id": context.tenant_id,
                    "idempotency_key": context.idempotency_key,
                },
            },
            headers={"Idempotency-Key": context.idempotency_key},
            timeout=definition.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    return Tool(definition, execute)


DatabaseHandler = Callable[[str, Mapping[str, Any], ToolContext], Awaitable[Any]]


def database_tool(
    name: str,
    handler: DatabaseHandler,
    *,
    read_only: bool = True,
    timeout_seconds: float = 30,
) -> Tool:
    async def execute(arguments: dict[str, Any], context: ToolContext) -> Any:
        return await handler(arguments["statement"], arguments.get("parameters", {}), context)

    return Tool(
        ToolDefinition(
            name=name,
            version="1.0.0",
            description="Execute a parameterized statement through a deployment-owned database adapter.",
            input_schema={
                "type": "object",
                "properties": {
                    "statement": {"type": "string", "minLength": 1},
                    "parameters": {"type": "object"},
                },
                "required": ["statement"],
                "additionalProperties": False,
            },
            output_schema={},
            required_permissions=frozenset({"database.read" if read_only else "database.write"}),
            side_effect=SideEffect.READ if read_only else SideEffect.WRITE,
            idempotency=Idempotency.IDEMPOTENT if read_only else Idempotency.KEYED,
            timeout_seconds=timeout_seconds,
        ),
        execute,
    )
