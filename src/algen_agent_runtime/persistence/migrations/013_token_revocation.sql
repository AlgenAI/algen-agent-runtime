-- Bearer token revocation (api.token_revocation). Entries are tenant-scoped. A jti entry is kept
-- only until the revoked token's own expiry; tokens themselves are never stored.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_revoked_tokens (
    tenant_id text NOT NULL,
    jti text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz NOT NULL,
    reason text,
    PRIMARY KEY (tenant_id, jti)
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_revoked_tokens_expiry_idx
    ON algen_agent_runtime_revoked_tokens (expires_at);
-- Every token for the subject issued (iat) before revoked_before is refused.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_revoked_subjects (
    tenant_id text NOT NULL,
    subject text NOT NULL,
    revoked_before timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    reason text,
    PRIMARY KEY (tenant_id, subject)
);
