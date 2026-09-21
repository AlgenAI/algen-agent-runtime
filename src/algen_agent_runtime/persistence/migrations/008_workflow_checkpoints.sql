CREATE TABLE IF NOT EXISTS algen_agent_runtime_workflow_checkpoints (
    id text NOT NULL,
    tenant_id text NOT NULL,
    manifest_name text NOT NULL,
    manifest_version text NOT NULL,
    status text NOT NULL,
    version integer NOT NULL,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS algen_agent_runtime_workflow_checkpoints_recovery_idx
    ON algen_agent_runtime_workflow_checkpoints (status, updated_at)
    WHERE status NOT IN ('completed', 'failed', 'cancelled');

CREATE INDEX IF NOT EXISTS algen_agent_runtime_workflow_checkpoints_tenant_idx
    ON algen_agent_runtime_workflow_checkpoints (tenant_id, updated_at DESC);
