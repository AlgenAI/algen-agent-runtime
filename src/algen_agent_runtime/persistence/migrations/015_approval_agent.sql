-- The agent whose run asked for an approval, for the approval inbox (GET /v1/approvals?agent=).
--
-- A column, not a field in the approval JSON: 0.1.0a17 reads that JSON with extra="forbid", so
-- rows written by this release stay readable by it during a rolling deploy or after a rollback.
-- Rows written before this migration have no agent (NULL).
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS agent text;

-- Listing a tenant's approvals for one agent, newest first.
CREATE INDEX IF NOT EXISTS algen_agent_runtime_approvals_agent_idx
    ON algen_agent_runtime_approvals (tenant_id, agent, created_at DESC);
