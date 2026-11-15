-- The user who owns an artifact: the uploader, or the user of the run that produced it. The API
-- routes let only the owner, the user of the artifact's run, or a principal with
-- artifacts:admin read, download or delete it, and erasing or exporting a user's data
-- (UserDataService) finds the user's artifacts by it. Additive: rows stored before this
-- migration keep NULL, and the routes fall back to the owner of their run (or artifacts:admin).
-- On a large artifacts table, create algen_agent_runtime_artifacts_user_idx with
-- CREATE INDEX CONCURRENTLY (same definition) before upgrading, so this migration finds it and
-- takes no long write lock.
ALTER TABLE algen_agent_runtime_artifacts
    ADD COLUMN IF NOT EXISTS user_id text;

CREATE INDEX IF NOT EXISTS algen_agent_runtime_artifacts_user_idx
    ON algen_agent_runtime_artifacts (tenant_id, user_id, created_at DESC)
    WHERE user_id IS NOT NULL;
