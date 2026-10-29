from __future__ import annotations

from typing import Any

import httpx

from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.security.network import (
    OutboundNetwork,
    create_safe_http_client,
    validate_outbound_url,
)
from algen_agent_runtime.security.sandbox import SandboxSettings, prepare_command, run_sandboxed
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)


def http_tool(
    *,
    allowed_hosts: tuple[str, ...],
    allow_private_networks: bool = False,
    client: httpx.AsyncClient | None = None,
    outbound: OutboundNetwork | None = None,
) -> Tool:
    http_client = client or create_safe_http_client(
        allowed_hosts=allowed_hosts,
        allow_private_networks=allow_private_networks,
        follow_redirects=False,
        outbound=outbound,
    )

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = arguments["url"]
        await validate_outbound_url(url, allowed_hosts, allow_private_networks)
        response = await http_client.request(
            arguments.get("method", "GET"),
            url,
            headers=arguments.get("headers"),
            json=arguments.get("json"),
        )
        return {
            "status": response.status_code,
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "etag"}
            },
            "body": response.text[:200_000],
        }

    return Tool(
        ToolDefinition(
            name="core.http",
            version="1.0.0",
            description="Call an allowlisted HTTP endpoint without following redirects.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    "headers": {"type": "object", "additionalProperties": {"type": "string"}},
                    "json": {},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["status", "headers", "body"]},
            required_permissions=frozenset({"network.http"}),
            side_effect=SideEffect.EXTERNAL,
            idempotency=Idempotency.KEYED,
        ),
        execute,
    )


def subprocess_tool(enabled: bool = False, sandbox: SandboxSettings | None = None) -> Tool:
    """Run one executable under ``sandbox`` (``security.sandbox``): no inherited environment
    unless its ``env_allowlist`` says otherwise, its limits and wrapper, a wall-clock limit,
    and the process group killed on timeout or cancellation."""
    policy = sandbox or SandboxSettings()

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if not enabled:
            raise PolicyDeniedError("subprocess tools are disabled")
        prepared = prepare_command(
            arguments["executable"],
            arguments.get("arguments", []),
            policy=policy,
            required=policy.required,
            label="core.subprocess",
        )
        exit_code, stdout, stderr = await run_sandboxed(
            prepared, timeout_seconds=policy.subprocess_timeout_seconds
        )
        return {
            "exit_code": exit_code,
            "stdout": stdout.decode(errors="replace")[:100_000],
            "stderr": stderr.decode(errors="replace")[:100_000],
        }

    return Tool(
        ToolDefinition(
            name="core.subprocess",
            version="1.0.0",
            description="Run an explicitly configured executable with no inherited environment.",
            input_schema={
                "type": "object",
                "properties": {
                    "executable": {"type": "string"},
                    "arguments": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["executable"],
                "additionalProperties": False,
            },
            output_schema={"type": "object", "required": ["exit_code", "stdout", "stderr"]},
            required_permissions=frozenset({"process.execute"}),
            side_effect=SideEffect.DESTRUCTIVE,
            idempotency=Idempotency.NON_IDEMPOTENT,
        ),
        execute,
    )
