-- Indexes that let erasing or exporting one user's data (UserDataService, the
-- `algen-agent-runtime users` command) find the user's runs and sessions without scanning the
-- tenant's. Forward-only and additive; harmless when unused. On a large runs table, create
-- algen_agent_runtime_runs_user_idx with CREATE INDEX CONCURRENTLY (same definition) before
-- upgrading, so this migration finds it and takes no long write lock.
CREATE INDEX IF NOT EXISTS algen_agent_runtime_runs_user_idx
    ON algen_agent_runtime_runs (tenant_id, (state->'request'->>'user_id'));

CREATE INDEX IF NOT EXISTS algen_agent_runtime_session_owners_user_idx
    ON algen_agent_runtime_session_owners (tenant_id, user_id);
