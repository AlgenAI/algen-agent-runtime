CREATE TABLE IF NOT EXISTS algen_agent_runtime_runs (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    version bigint NOT NULL DEFAULT 0,
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_runs_tenant_idx ON algen_agent_runtime_runs (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_events (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES algen_agent_runtime_runs(id),
    tenant_id text NOT NULL,
    sequence bigint NOT NULL,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, sequence)
);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_audit_events (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    event jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_audit_tenant_idx ON algen_agent_runtime_audit_events (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_approvals (
    id text PRIMARY KEY,
    run_id text NOT NULL REFERENCES algen_agent_runtime_runs(id),
    tenant_id text NOT NULL,
    approval jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_approvals_tenant_idx
    ON algen_agent_runtime_approvals (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_memory (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL,
    session_id text NOT NULL,
    message jsonb NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_memory_session_idx
    ON algen_agent_runtime_memory (tenant_id, session_id, id DESC);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_artifacts (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    run_id text NOT NULL REFERENCES algen_agent_runtime_runs(id),
    name text NOT NULL,
    media_type text NOT NULL,
    data bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_artifacts_tenant_idx
    ON algen_agent_runtime_artifacts (tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_tool_executions (
    idempotency_key text PRIMARY KEY,
    run_id text NOT NULL REFERENCES algen_agent_runtime_runs(id),
    step_id text NOT NULL,
    tenant_id text NOT NULL,
    tool_name text NOT NULL,
    status text NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_tool_executions_run_idx
    ON algen_agent_runtime_tool_executions (tenant_id, run_id, created_at DESC);
