-- Indexes that let retention sweeps find expired rows and delete run-scoped children
-- without sequential scans. Sweeps remain opt-in; these indexes are harmless when unused.
CREATE INDEX IF NOT EXISTS algen_agent_runtime_runs_updated_idx
    ON algen_agent_runtime_runs (updated_at);

CREATE INDEX IF NOT EXISTS algen_agent_runtime_approvals_run_idx
    ON algen_agent_runtime_approvals (run_id);

CREATE INDEX IF NOT EXISTS algen_agent_runtime_tool_executions_run_id_idx
    ON algen_agent_runtime_tool_executions (run_id);

CREATE INDEX IF NOT EXISTS algen_agent_runtime_memory_expiry_idx
    ON algen_agent_runtime_memory (expires_at)
    WHERE expires_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS algen_agent_runtime_audit_created_idx
    ON algen_agent_runtime_audit_events (created_at);

