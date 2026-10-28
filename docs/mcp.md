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
- **`auth`** (`type: oauth`): OAuth for servers that accept only short-lived, per-user OAuth
  tokens. See [OAuth for remote MCP servers](#oauth-for-remote-mcp-servers). It cannot be combined
  with `auth_token_ref` or an `Authorization` secret header.

## OAuth for remote MCP servers

Hosted MCP servers such as Slack, Linear and Jira accept only OAuth access tokens. With `auth`
on a `streamable_http` or `sse` server, the Runtime runs the OAuth 2.1 authorization code flow
with PKCE (S256), stores the tokens for each tenant (or each user), refreshes them, and sends the
access token on every request to the server. Fixed bearer tokens (`auth_token_ref`) keep working
as before; a server without `auth` behaves exactly as it did.

```yaml
storage:
  postgres_dsn: env://RUNTIME_POSTGRES_DSN
  mcp_oauth_token_store: postgres            # memory (default) or postgres
  mcp_oauth_encryption_key: env://MCP_TOKEN_KEY
  # mcp_oauth_previous_encryption_keys: [env://MCP_TOKEN_KEY_2025]   # during a key rotation

mcp_servers:
  # Metadata discovery (RFC 9728 + RFC 8414): only the client and redirect are configured.
  - name: linear
    transport: streamable_http
    url: https://mcp.linear.app/mcp
    auth:
      type: oauth
      client_id_ref: env://LINEAR_CLIENT_ID
      client_secret_ref: env://LINEAR_CLIENT_SECRET
      scopes: [read, write]
      token_scope: user                       # one connection per user; tenant is the default
      redirect_uri: https://agents.example.com/v1/mcp/oauth/callback
      success_redirect_url: https://studio.example.com/settings/connections

  # Explicit endpoints, for a provider that publishes no OAuth metadata.
  - name: slack
    transport: streamable_http
    url: https://mcp.slack.example.com/mcp
    auth:
      type: oauth
      client_id: "1234567890.0987654321"
      client_secret_ref: env://SLACK_CLIENT_SECRET
      token_endpoint_auth_method: client_secret_post
      authorization_endpoint: https://slack.com/oauth/v2_user/authorize
      token_endpoint: https://slack.com/api/oauth.v2.user.access
      revocation_endpoint: https://slack.com/api/auth.revoke
      scopes: [channels:history, chat:write]
      token_scope: user
      send_resource: false                    # for a provider that rejects RFC 8707 'resource'
      redirect_uri: https://agents.example.com/v1/mcp/oauth/callback

  # A public client (no secret) relies on PKCE alone; a tenant-wide connection is the default.
  - name: jira
    transport: streamable_http
    url: https://mcp.atlassian.example.com/v1/mcp
    auth:
      type: oauth
      client_id: jira-runtime
      scopes: [read:jira-work, write:jira-work, offline_access]
      redirect_uri: https://agents.example.com/v1/mcp/oauth/callback
      discovery_tenant_id: acme             # lists tools at startup through this connection
```

The endpoint values above are illustrative; use the ones your provider documents.

| `auth` field | Default | Description |
| --- | --- | --- |
| `type` | `oauth` | The only auth kind. |
| `client_id` / `client_id_ref` | *one required* | The registered client ID, literal or `env://`. |
| `client_secret_ref` | none | `env://` client secret. Unset makes a public client (PKCE only). |
| `token_endpoint_auth_method` | `client_secret_post` | Or `client_secret_basic`, for confidential clients. |
| `scopes` | `[]` | Scopes requested on the authorization request. |
| `token_scope` | `tenant` | `tenant`: one connection per tenant. `user`: one per user; a call without a `user_id` fails closed. |
| `redirect_uri` | *required* | The callback registered with the provider. It must reach `GET /v1/mcp/oauth/callback`; only this exact value is sent. HTTPS (HTTP only for localhost). |
| `authorization_endpoint`, `token_endpoint` | discovered | Set both to skip discovery. |
| `revocation_endpoint` | discovered | RFC 7009 endpoint called on revoke, if any. |
| `issuer` | the MCP server's origin | Authorization server to discover when the MCP server publishes no protected resource metadata. |
| `send_resource` | `true` | Send the RFC 8707 `resource` parameter (the MCP server URL). |
| `authorize_params` | `{}` | Extra non-secret authorization parameters (e.g. `prompt: consent`). Parameters the Runtime sets are rejected. |
| `success_redirect_url` | none | Where the callback sends the browser, with `mcp_server`, `status` (`connected` or `error`) and `error` added. Without it the callback answers with JSON. |
| `state_ttl_seconds` | `600` | How long a started authorization stays valid (30 to 3600). |
| `refresh_margin_seconds` | `60` | Refresh this long before the access token expires. |
| `discovery_tenant_id`, `discovery_user_id` | none | Whose stored connection lists the server's tools at startup. |

### Connecting: the HTTP routes

| Route | Scope | What it does |
| --- | --- | --- |
| `POST /v1/mcp/servers/{server}/oauth/start` | `mcp:connect` (+ `mcp:admin` for `token_scope: tenant`) | Returns `{server, token_scope, authorize_url, expires_at}`. Send the user's browser to `authorize_url`. |
| `GET /v1/mcp/oauth/callback` | none: the state is the credential | The provider redirects here. Exchanges the code, stores the tokens, lists the server's tools if nobody has yet, and redirects to `success_redirect_url` or answers with the connection status. |
| `GET /v1/mcp/servers/{server}/oauth/status` | `mcp:read` or `mcp:connect` | `{server, token_scope, connected, expires_at, scopes, refreshable, connected_at, connected_by}`. |
| `GET /v1/mcp/oauth/servers` | `mcp:read` or `mcp:connect` | The caller's status for every OAuth server. |
| `DELETE /v1/mcp/servers/{server}/oauth` | `mcp:connect` (+ `mcp:admin` for tenant scope) | Removes the stored tokens, closes the caller's open session, then calls the provider's revocation endpoint if one is configured or advertised. Returns `{server, removed, provider_revoked}` (`provider_revoked` is `null` when there was nothing to revoke there). |

A tenant-scoped connection is shared by everyone in the tenant, so starting or revoking it needs
`mcp:admin` as well. No response carries a token.

### When a user has not connected

A tool call on a server with no usable connection for the caller raises `MCPNotConnectedError`
(a `ToolExecutionError`). The Runtime always answers that call to the model with a clean tool
error, whatever the agent's `tool_policy.on_error`:

```json
{"error": {"type": "MCPNotConnected", "server": "linear",
           "message": "MCP server 'linear' is not connected for this user; ask the user to connect it, then try again"}}
```

It also emits an `mcp.auth_required` run event with what a UI needs for a Connect button, and
writes a `tool.call` audit record with outcome `not_connected`:

```json
{"code": "mcp_not_connected", "server": "linear", "token_scope": "user",
 "start_path": "/v1/mcp/servers/linear/oauth/start"}
```

A run without a `user_id` against a `token_scope: user` server is refused with
`PolicyDeniedError`; it never falls back to another user's or the tenant's connection.

### Tools of an OAuth server

At startup there is usually no connection to list tools with, so the server is recorded in
`container.degraded_dependencies["mcp.<name>"]` (startup does not fail, even with
`required: true`) and its tools are listed when the first connection completes. Set
`discovery_tenant_id` (and `discovery_user_id` for per-user servers) to list them at startup
through that stored connection instead. Tool definitions are shared by all tenants, like any
registered tool; every call runs on the caller's own connection, opened on first use (at most
256 open sessions, least recently used closed first).

### Storage, encryption and refresh

- `mcp_oauth_token_store: memory` keeps tokens in process memory: a restart forgets every
  connection, and a callback must reach the worker that started the flow. Use it for
  development and single-process deployments.
- `mcp_oauth_token_store: postgres` stores token sets and pending authorizations in
  `algen_agent_runtime_mcp_oauth_tokens` and `algen_agent_runtime_mcp_oauth_states`
  (migration `012_mcp_oauth.sql`) as AES-256-GCM ciphertext. Each value's associated data is its
  row key (tenant, user, server), so a value copied into another tenant's row fails to decrypt.
  The OAuth state itself is never stored, only its SHA-256. This needs the `encryption` extra
  (`pip install 'algen-agent-runtime[encryption]'`) and `mcp_oauth_encryption_key`, an `env://`
  reference to a base64 32-byte key (`python -c "from algen_agent_runtime.security.encryption
  import generate_key; print(generate_key())"`). To rotate, set the new key and list the old one
  under `mcp_oauth_previous_encryption_keys`; values are re-encrypted with the new key as tokens
  refresh. `SecretCipher` is the protocol to implement for a KMS-backed cipher.
- Access tokens are refreshed `refresh_margin_seconds` before they expire, and once more when
  the server answers 401. A refresh the provider rejects (HTTP 400/401, such as `invalid_grant`)
  removes the connection, so the next call reports "not connected"; a provider outage keeps it
  and the call fails as a retryable tool error.
- Metadata, token and revocation requests go through the Runtime's SSRF-guarded HTTP client:
  add the authorization server's hosts to `security.allowed_http_hosts` when you use an
  allowlist.

### Security properties

- Tokens, refresh tokens, PKCE verifiers and client secrets never reach tool code, the model,
  logs, run events, spans or audit records; token values are `SecretStr` in memory.
- The state is 256 random bits, bound to the tenant, user and server that started the flow,
  valid for `state_ttl_seconds`, and deleted on first use, so a replayed or forged callback is
  rejected (`400`, `code: mcp_oauth_invalid_state`).
- Only the configured `redirect_uri` is ever sent, and a pending authorization is rejected if
  the configured value changed since it started. Callback responses are `Cache-Control:
  no-store` and `Referrer-Policy: no-referrer`.
- Discovered metadata must name the issuer it was fetched for (RFC 8414 section 3.3), protected
  resource metadata must be for this server, and every endpoint must be HTTPS.
- One tenant can never read another's tokens: every lookup is keyed by tenant.

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
- **OAuth dynamic client registration**: OAuth servers need a client registered with the
  provider (`client_id`); RFC 7591 registration and client ID metadata documents are not
  supported yet. The SDK's `OAuthClientProvider` is not used because it runs the whole flow
  inside one request of one process and keeps one token set per instance; the Runtime splits
  the flow across its start and callback routes and keeps tokens per tenant or user.
- **MCP Python SDK v2**: Runtime uses the validated `mcp>=1.30.0,<2` line to retain `httpx`-based `SafeAsyncTransport` and connection security. Migration to SDK v2 (which uses `httpx2`) is tracked for a subsequent release.
