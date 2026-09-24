# Model Context Protocol (MCP) Integration

Algen Agent Runtime provides a managed client connector for the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It enables agents and multi-agent workflows to consume tools exposed by external MCP servers across standard transports under unified Runtime governance, outbound network security, and policy boundaries.

## Architecture and Scope

- **Role**: Runtime operates strictly as an **MCP Client**. It connects to MCP servers, discovers their tools, translates input/output schemas into `ToolDefinition` contracts, and routes tool calls through the standard `ToolExecutor` (enforcing policy decisions, approvals, audit logging, and idempotency).
- **Client Protocol Implementation**: Runtime builds directly on the official [`mcp`](https://pypi.org/project/mcp/) Python SDK v1 line (`mcp>=1.30.0,<2`).
- **Optional Dependency**: MCP client capabilities are packaged under the optional extra `mcp`:
  ```bash
  python -m pip install 'algen-agent-runtime[mcp]'
  ```
  Core Runtime components and configuration loading remain functional without `mcp` installed.

## Declarative YAML Configuration

MCP servers are configured declaratively in Runtime YAML under the top-level `mcp_servers` section:

```yaml
mcp_servers:
  - name: local-files
    transport: stdio
    command: npx
    args: [-y, "@modelcontextprotocol/server-filesystem", /workspace]
    allowed_tools: [read_file, list_directory]
    required: true
    tool_policies:
      read_file:
        side_effect: read
        idempotency: idempotent
      list_directory:
        side_effect: read
        idempotency: idempotent

  - name: inventory
    transport: streamable_http
    url: https://mcp.example.com/mcp
    auth_token_ref: env://MCP_INVENTORY_TOKEN
    allowed_tools: [lookup_item, update_stock]
    required: true
    trust_tool_annotations: false
    tool_policies:
      update_stock:
        side_effect: write
        idempotency: non_idempotent
```

### Server Configuration Schema

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `name` | `str` | *required* | Unique server identifier (`^[a-z0-9_.-]+$`). |
| `transport` | `stdio`, `streamable_http`, `sse` | *required* | Transport mechanism. `streamable_http` is the modern standard; `sse` is retained for backwards compatibility. |
| `required` | `bool` | `false` | If `true`, container startup fails if discovery or connection fails. If `false`, server degrades gracefully and records failure under `container.degraded_dependencies`. |
| `allowed_tools` | `list[str]` | `["*"]` | Allowlist of tool names exposed to Runtime agents. |
| `prefix` | `str` | `mcp.<name>.` | Prefix for registered tool names. |
| `trust_tool_annotations` | `bool` | `false` | Whether to honor server-provided hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`). |
| `tool_policies` | `dict[str, MCPToolPolicyOverride]` | `{}` | Operator-defined governance policy overrides per tool. |
| `timeout_seconds` | `float` | `30.0` | Default timeout for tool invocations. |
| `max_result_bytes` | `int` | `1,048,576` (1MB) | Maximum payload size before execution is aborted. |

### Transports

1. **`stdio`**: Spawns a local subprocess and communicates via standard input/output.
   - Requires `command: str` and optional `args: list[str]`.
   - **Environment Isolation**: Subprocesses do not inherit ambient process environment variables. Only safe system keys (`PATH`, `SYSTEMROOT`, `TEMP`, `USER`, `HOME`) and explicit variables in `env` are passed.
   - **Trust Boundary**: `stdio` commands run unsandboxed on the host system. They must only execute trusted operator-declared commands.
2. **`streamable_http`**: Connects via HTTP using the official SDK v1 Streamable HTTP client.
   - Requires `url: str`.
   - Outbound connections are routed through Runtime's `create_safe_http_client`, enforcing DNS pinning, host allowlists (`security.allowed_http_hosts`), and private network blocking (`security.allow_private_networks`).
3. **`sse`**: Connects via legacy Server-Sent Events HTTP streaming.
   - Retained for compatibility with older servers; superseded by `streamable_http`. Also enforces Runtime outbound network controls.

## Authentication and Secret Hygiene

To prevent credential leakage in configuration dumps, telemetry spans, or error messages:

- **`auth_token_ref`**: Bearer token specified as an `env://VAR_NAME` reference, resolved at connection time.
- **`secret_headers`**: Custom headers with secret values, each declared as an `env://VAR_NAME` reference (e.g. `X-Custom-Auth: env://CUSTOM_KEY`).
- **Literal Header Rejection**: Literal sensitive headers (`Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`) in `headers` are rejected at configuration validation time.
- **`headers`**: Reserved solely for non-secret headers, such as protocol or client versions.

## Governance and Policy Classification

MCP tools discovered by Runtime are assigned `SideEffect` and `Idempotency` classifications using the following precedence:

1. **Operator `tool_policies` Override (Authoritative)**:
   Explicit policy defined in YAML takes precedence over all server hints.
2. **Trusted Server Annotations**:
   When `trust_tool_annotations: true` is configured on the server, MCP `ToolAnnotations` are mapped conservatively:
   - `destructiveHint: true` → `SideEffect.DESTRUCTIVE`
   - `readOnlyHint: true` (and not destructive) → `SideEffect.READ`
   - `openWorldHint: true` (without read-only) → `SideEffect.EXTERNAL`
   - `idempotentHint: true` → `Idempotency.IDEMPOTENT`
   - Absent or false hints → `Idempotency.NON_IDEMPOTENT`
3. **Secure Defaults**:
   When annotations are absent or untrusted (`trust_tool_annotations: false`), tools default to `SideEffect.EXTERNAL` and `Idempotency.NON_IDEMPOTENT`.

The classification source (`operator`, `trusted_annotation`, or `secure_default`) is attached to tool audit metadata.

## Container Lifecycle and Startup Behavior

During `Container.astart()`:
1. All declared MCP servers are connected and their tools are paginated and discovered.
2. Tool definitions are validated and registered into `container.tools`.
3. If a server with `required: true` fails discovery, startup aborts with a sanitized `ConfigurationError`.
4. If a server with `required: false` fails discovery, startup continues, the error is recorded in `container.degraded_dependencies["mcp.<name>"]`, and no tools from that server are registered.
5. On `Container.aclose()`, all active MCP sessions, transport streams, and child processes are terminated cleanly.

## Current Boundaries and Deferred Capabilities

The following capabilities are outside the scope of `0.1.0a3`:
- **Unsandboxed Local Processes**: `stdio` transport runs host processes directly without sandbox containment; do not configure untrusted or dynamic user commands.
- **MCP Server Hosting**: Runtime acts solely as an MCP client; exposing Runtime tools or agents as an MCP server is deferred.
- **Resources, Prompts, and Sampling Callbacks**: Only the MCP Tools primitive is supported.
- **MCP Python SDK v2**: Runtime uses the validated `mcp>=1.30.0,<2` line to retain `httpx`-based `SafeAsyncTransport` and connection security. Migration to SDK v2 (which uses `httpx2`) is tracked for a subsequent release.
