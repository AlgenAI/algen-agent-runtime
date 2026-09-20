CREATE TABLE IF NOT EXISTS algen_agent_runtime_analytical_graphs (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    status text NOT NULL,
    version bigint NOT NULL DEFAULT 0,
    state jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_analytical_graphs_tenant_idx
    ON algen_agent_runtime_analytical_graphs (tenant_id, status, updated_at);

CREATE TABLE IF NOT EXISTS algen_agent_runtime_work_items (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    kind text NOT NULL,
    deduplication_key text NOT NULL,
    status text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    maximum_attempts integer NOT NULL DEFAULT 3,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    result jsonb,
    error text,
    item jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, deduplication_key)
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_work_items_claim_idx
    ON algen_agent_runtime_work_items (status, available_at, lease_expires_at);
