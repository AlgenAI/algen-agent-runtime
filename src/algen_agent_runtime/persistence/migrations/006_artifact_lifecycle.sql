ALTER TABLE algen_agent_runtime_artifacts
    ALTER COLUMN run_id DROP NOT NULL;

ALTER TABLE algen_agent_runtime_artifacts
    DROP CONSTRAINT IF EXISTS algen_agent_runtime_artifacts_run_id_fkey;

ALTER TABLE algen_agent_runtime_artifacts
    ADD CONSTRAINT algen_agent_runtime_artifacts_run_id_fkey
    FOREIGN KEY (run_id) REFERENCES algen_agent_runtime_runs(id) ON DELETE SET NULL;

ALTER TABLE algen_agent_runtime_artifacts
    ADD COLUMN IF NOT EXISTS size_bytes bigint,
    ADD COLUMN IF NOT EXISTS sha256 text,
    ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'available',
    ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS expires_at timestamptz;

UPDATE algen_agent_runtime_artifacts
SET size_bytes = octet_length(data),
    sha256 = encode(sha256(data), 'hex')
WHERE size_bytes IS NULL OR sha256 IS NULL;

ALTER TABLE algen_agent_runtime_artifacts
    ALTER COLUMN size_bytes SET NOT NULL,
    ALTER COLUMN sha256 SET NOT NULL;

ALTER TABLE algen_agent_runtime_artifacts
    ADD CONSTRAINT algen_agent_runtime_artifacts_status_check
    CHECK (status IN ('pending_scan', 'available', 'quarantined'));

CREATE INDEX IF NOT EXISTS algen_agent_runtime_artifacts_expiry_idx
    ON algen_agent_runtime_artifacts (expires_at)
    WHERE expires_at IS NOT NULL;
