-- MCP OAuth connections. Token sets and pending authorizations are stored only as
-- authenticated ciphertext whose associated data is the row key, so a value copied to
-- another tenant's row fails to decrypt. user_id is '' for tenant-scoped connections.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_mcp_oauth_tokens (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    server text NOT NULL,
    ciphertext text NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, user_id, server)
);
-- state_hash is the SHA-256 of the OAuth state; the state itself is never stored.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_mcp_oauth_states (
    state_hash text PRIMARY KEY,
    tenant_id text NOT NULL,
    server text NOT NULL,
    ciphertext text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_mcp_oauth_states_expiry_idx
    ON algen_agent_runtime_mcp_oauth_states (expires_at);
