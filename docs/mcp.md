# Model Context Protocol (MCP) Client Integration

Algen Agent Runtime provides a managed client connector for the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/). It enables agents and multi-agent workflows to consume external tools exposed by standard MCP servers across stdio and streamable HTTP/Server-Sent Events (SSE) transports.

## Architecture and Scope

- **Role**: Runtime operates strictly as an **MCP Client**. It discovers external tools, transforms their schemas into Algen `ToolDefinition` contracts, and manages connection and process lifecycles. (Exposing Runtime-internal tools as an MCP server is intentionally deferred to preserve security boundaries).
- **Optional Dependency**: MCP client capabilities are packaged under the optional extra `mcp`. Install with:
  ```bash
  python -m pip install 'algen-agent-runtime[mcp]'
  ```
  Core Runtime components remain fully functional and tested without the `mcp` package installed.

## Transports and Configurations

Runtime supports two standard MCP transports via the official `mcp` Python SDK:

### 1. Stdio Transport (`MCPStdioServerConfig`)

Spawns a local subprocess and communicates via standard input/output JSON-RPC:

```python
from algen_agent_runtime.mcp import MCPStdioServerConfig

config = MCPStdioServerConfig(
    name="filesystem_server",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/allowed/workspace"],
    allowed_tools=frozenset({"read_file", "list_directory"}), # or frozenset({"*"})
    timeout_seconds=15.0,
    max_result_bytes=524_288, # 512 KB
    env={"NODE_ENV": "production"},
)
```

### 2. SSE / Streamable HTTP Transport (`MCPSseServerConfig`)

Connects to a remote HTTP server streaming events over SSE:

```python
from algen_agent_runtime.mcp import MCPSseServerConfig

config = MCPSseServerConfig(
    name="remote_service",
    url="https://mcp.internal.corp/sse",
    auth_token_ref="env://MCP_AUTH_TOKEN", # Resolved securely from environment
    headers={"X-Client-Version": "1.0"},
    timeout_seconds=30.0,
    max_result_bytes=1_048_576, # 1 MB
)
```

## Security Boundaries & Hardening

1. **Child Process Environment Isolation**:
   By default, child processes spawned under stdio transport **never inherit ambient process environment variables** (preventing accidental leakage of database credentials, cloud access tokens, or private keys). Only minimal system variables (`PATH`, `SYSTEMROOT`, `TEMP`, `USER`, `HOME`) and explicit variables declared in `config.env` are passed.
2. **Secret References for Remote Authentication**:
   Remote authentication tokens must use `env://` secret references; literal secrets are rejected.
3. **Per-Server and Per-Tool Allowlists**:
   Each server declaration supports `allowed_tools`. Discovered tools not present in the allowlist are omitted from discovery.
4. **Collision-Proof Tool Namespacing**:
   Discovered tools are namespaced by default as `mcp.<server_name>.<tool_name>`, preventing collisions with built-in runtime tools or other MCP servers.
5. **Execution Bounds & Timeout Enforcement**:
   Calls are governed by `timeout_seconds` and `max_result_bytes`. Oversized payloads and hanging processes are aborted.
6. **Error Redaction**:
   Server crash messages and internal traces are sanitized to content-minimal summaries before returning to the model or user.

## Using Discovered MCP Tools in Agents

Tools can be discovered and registered directly into the container or tool registry:

```python
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.mcp import MCPClientManager, MCPStdioServerConfig

server = MCPStdioServerConfig(
    name="weather",
    command="python",
    args=["weather_server.py"],
)

# Option A: Wire into container
container = build_container(settings, mcp_servers=(server,))

# Option B: Explicit discovery and registration
manager = MCPClientManager((server,))
discovered_tools = await manager.discover_tools()
for tool in discovered_tools:
    container.tools.register(tool)
```

To allow an agent to use a discovered MCP tool, declare the namespaced name in `agent.yaml`:

```yaml
agents:
  - name: assistant
    version: 1.0.0
    enabled_tools:
      - mcp.weather.get_forecast
    tool_permissions:
      - mcp.weather
```
