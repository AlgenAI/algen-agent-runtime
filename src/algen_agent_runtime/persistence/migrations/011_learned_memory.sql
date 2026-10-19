-- Learned records survive source-run retention. Provenance IDs deliberately have
-- no run foreign key. Content-free revisions and operation receipts prevent stale
-- writes from resurrecting corrected, expired, or deleted memories.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_learned_memory (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    agent_id text NOT NULL,
    key text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    record jsonb NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    expires_at timestamptz,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, user_id, agent_id, key)
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_learned_memory_recall_idx
    ON algen_agent_runtime_learned_memory (tenant_id, user_id, agent_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_learned_memory_expiry_idx
    ON algen_agent_runtime_learned_memory (expires_at) WHERE expires_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS algen_agent_runtime_memory_operations (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    agent_id text NOT NULL,
    operation_id text NOT NULL,
    fingerprint text NOT NULL,
    receipt jsonb NOT NULL,
    PRIMARY KEY (tenant_id, user_id, agent_id, operation_id)
);
