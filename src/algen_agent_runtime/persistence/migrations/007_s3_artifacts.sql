ALTER TABLE algen_agent_runtime_artifacts
    ADD COLUMN IF NOT EXISTS storage_backend text NOT NULL DEFAULT 'postgres',
    ADD COLUMN IF NOT EXISTS storage_key text;

ALTER TABLE algen_agent_runtime_artifacts
    ALTER COLUMN data DROP NOT NULL;

UPDATE algen_agent_runtime_artifacts
SET storage_backend = 'postgres'
WHERE storage_backend IS NULL;

ALTER TABLE algen_agent_runtime_artifacts
    ADD CONSTRAINT algen_agent_runtime_artifacts_storage_check
    CHECK (
        (storage_backend = 'postgres' AND data IS NOT NULL AND storage_key IS NULL)
        OR
        (storage_backend = 's3' AND data IS NULL AND storage_key IS NOT NULL)
    );

CREATE INDEX IF NOT EXISTS algen_agent_runtime_artifacts_storage_idx
    ON algen_agent_runtime_artifacts (storage_backend, tenant_id, created_at DESC);
