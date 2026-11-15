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

MAX_HTTP_BODY_BYTES = 200_000
MAX_SUBPROCESS_OUTPUT_BYTES = 1_000_000


def http_tool(
    *,
    allowed_hosts: tuple[str, ...],
    allow_private_networks: bool = False,
    allow_any_public_host: bool = False,
    client: httpx.AsyncClient | None = None,
    outbound: OutboundNetwork | None = None,
) -> Tool:
    """``core.http``: one request to an allowlisted host, without following redirects.

    With no ``allowed_hosts`` the tool refuses every call unless ``allow_any_public_host``
    (``security.allow_any_public_host``) says the model may reach any public host. The body is
    streamed and cut at ``MAX_HTTP_BODY_BYTES`` (``truncated: true``).
    """
    http_client = client or create_safe_http_client(
        allowed_hosts=allowed_hosts,
        allow_private_networks=allow_private_networks,
        follow_redirects=False,
        outbound=outbound,
    )

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        if not allowed_hosts and not allow_any_public_host:
            raise PolicyDeniedError(
                "core.http has no allowed hosts: list them in security.allowed_http_hosts, or "
                "set security.allow_any_public_host: true to let the model reach any public host"
            )
        url = arguments["url"]
        await validate_outbound_url(url, allowed_hosts, allow_private_networks)
        async with http_client.stream(
            arguments.get("method", "GET"),
            url,
            headers=arguments.get("headers"),
            json=arguments.get("json"),
        ) as response:
            body = bytearray()
            truncated = False
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_HTTP_BODY_BYTES:
                    truncated = True
                    break
            encoding = response.charset_encoding or "utf-8"
            try:
                text = bytes(body[:MAX_HTTP_BODY_BYTES]).decode(encoding, errors="replace")
            except LookupError:
                text = bytes(body[:MAX_HTTP_BODY_BYTES]).decode("utf-8", errors="replace")
            return {
                "status": response.status_code,
                "headers": {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() in {"content-type", "etag"}
                },
                "body": text,
                "truncated": truncated,
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
            output_schema={
                "type": "object",
                "properties": {"truncated": {"type": "boolean"}},
                "required": ["status", "headers", "body"],
            },
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
        executable = arguments["executable"]
        if not policy.executable_allowed(executable):
            raise PolicyDeniedError(
                f"core.subprocess may not run {executable!r}; list its absolute path in "
                "security.sandbox.allowed_executables"
                if policy.allowed_executables
                else "core.subprocess has no allowed executables; list their absolute paths in "
                "security.sandbox.allowed_executables"
            )
        prepared = prepare_command(
            executable,
            arguments.get("arguments", []),
            policy=policy,
            required=policy.required,
            label="core.subprocess",
        )
        exit_code, stdout, stderr = await run_sandboxed(
            prepared,
            timeout_seconds=policy.subprocess_timeout_seconds,
            max_output_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
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
            description="Run one of the configured executables (security.sandbox."
            "allowed_executables, by absolute path) with no inherited environment.",
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
