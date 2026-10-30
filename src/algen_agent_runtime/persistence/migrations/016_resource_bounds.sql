-- Per-tenant storage usage for quotas, and indexes for the conversation, workflow checkpoint,
-- analytical graph and work item retention sweeps.
--
-- Triggers keep algen_agent_runtime_tenant_usage in step with every insert, update and delete of
-- artifacts, session memory and learned memory, whichever code path (or operator) makes it, in
-- the same transaction. A quota-checked write locks the tenant's usage row first, so concurrent
-- writes for one tenant and resource are serialized and the check sees every committed change.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_tenant_usage (
    tenant_id text NOT NULL,
    resource text NOT NULL,
    items bigint NOT NULL DEFAULT 0,
    bytes bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, resource)
);

-- SET search_path FROM CURRENT pins the schema the migration ran in, so the triggers find the
-- usage table whatever search_path the session that fires them has.
CREATE OR REPLACE FUNCTION algen_agent_runtime_add_usage(
    p_tenant_id text, p_resource text, p_items bigint, p_bytes bigint
) RETURNS void LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF p_items = 0 AND p_bytes = 0 THEN
        RETURN;
    END IF;
    INSERT INTO algen_agent_runtime_tenant_usage AS u (tenant_id, resource, items, bytes)
    VALUES (p_tenant_id, p_resource, p_items, p_bytes)
    ON CONFLICT (tenant_id, resource) DO UPDATE
    SET items = u.items + EXCLUDED.items, bytes = u.bytes + EXCLUDED.bytes, updated_at = now();
END $$;

CREATE OR REPLACE FUNCTION algen_agent_runtime_track_artifact_usage() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM algen_agent_runtime_add_usage(OLD.tenant_id, 'artifacts', -1, -OLD.size_bytes);
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        PERFORM algen_agent_runtime_add_usage(NEW.tenant_id, 'artifacts', 1, NEW.size_bytes);
    END IF;
    RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION algen_agent_runtime_track_memory_usage() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        PERFORM algen_agent_runtime_add_usage(
            OLD.tenant_id, 'memory', -1, -octet_length(OLD.message::text)
        );
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        PERFORM algen_agent_runtime_add_usage(
            NEW.tenant_id, 'memory', 1, octet_length(NEW.message::text)
        );
    END IF;
    RETURN NULL;
END $$;

-- Learned memory counts records that still hold content: deleted records and records whose
-- expired content was purged are revisions only and do not count.
CREATE OR REPLACE FUNCTION algen_agent_runtime_track_learned_memory_usage() RETURNS trigger
LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE')
        AND NOT OLD.deleted AND OLD.record->>'content' IS NOT NULL THEN
        PERFORM algen_agent_runtime_add_usage(
            OLD.tenant_id, 'learned_memory', -1, -octet_length(OLD.record::text)
        );
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE')
        AND NOT NEW.deleted AND NEW.record->>'content' IS NOT NULL THEN
        PERFORM algen_agent_runtime_add_usage(
            NEW.tenant_id, 'learned_memory', 1, octet_length(NEW.record::text)
        );
    END IF;
    RETURN NULL;
END $$;

-- Creating the triggers locks each table against writes until this migration commits, so the
-- backfill below counts exactly the rows that existed before the triggers took over.
DROP TRIGGER IF EXISTS algen_agent_runtime_artifact_usage ON algen_agent_runtime_artifacts;
CREATE TRIGGER algen_agent_runtime_artifact_usage
    AFTER INSERT OR DELETE OR UPDATE OF tenant_id, size_bytes ON algen_agent_runtime_artifacts
    FOR EACH ROW EXECUTE FUNCTION algen_agent_runtime_track_artifact_usage();

DROP TRIGGER IF EXISTS algen_agent_runtime_memory_usage ON algen_agent_runtime_memory;
CREATE TRIGGER algen_agent_runtime_memory_usage
    AFTER INSERT OR DELETE OR UPDATE OF tenant_id, message ON algen_agent_runtime_memory
    FOR EACH ROW EXECUTE FUNCTION algen_agent_runtime_track_memory_usage();

DROP TRIGGER IF EXISTS algen_agent_runtime_learned_memory_usage
    ON algen_agent_runtime_learned_memory;
CREATE TRIGGER algen_agent_runtime_learned_memory_usage
    AFTER INSERT OR DELETE OR UPDATE ON algen_agent_runtime_learned_memory
    FOR EACH ROW EXECUTE FUNCTION algen_agent_runtime_track_learned_memory_usage();

DELETE FROM algen_agent_runtime_tenant_usage
WHERE resource IN ('artifacts', 'memory', 'learned_memory');
INSERT INTO algen_agent_runtime_tenant_usage (tenant_id, resource, items, bytes)
SELECT tenant_id, 'artifacts', count(*), COALESCE(sum(size_bytes), 0)
FROM algen_agent_runtime_artifacts GROUP BY tenant_id;
INSERT INTO algen_agent_runtime_tenant_usage (tenant_id, resource, items, bytes)
SELECT tenant_id, 'memory', count(*), COALESCE(sum(octet_length(message::text)), 0)
FROM algen_agent_runtime_memory GROUP BY tenant_id;
INSERT INTO algen_agent_runtime_tenant_usage (tenant_id, resource, items, bytes)
SELECT tenant_id, 'learned_memory', count(*), COALESCE(sum(octet_length(record::text)), 0)
FROM algen_agent_runtime_learned_memory
WHERE NOT deleted AND record->>'content' IS NOT NULL
GROUP BY tenant_id;

-- Retention sweeps: find old finished records, and delete a conversation's children (and check
-- the foreign keys that point at them) without sequential scans. Harmless when unused; on large
-- existing tables, create them CONCURRENTLY before upgrading to avoid a long write lock.
CREATE INDEX IF NOT EXISTS algen_agent_runtime_conversations_updated_idx
    ON algen_agent_runtime_conversations (updated_at);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_conversation_messages_conversation_idx
    ON algen_agent_runtime_conversation_messages (conversation_id);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_conversation_feedback_conversation_id_idx
    ON algen_agent_runtime_conversation_feedback (conversation_id);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_conversation_feedback_message_idx
    ON algen_agent_runtime_conversation_feedback (message_id);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_workflow_checkpoints_terminal_idx
    ON algen_agent_runtime_workflow_checkpoints (updated_at)
    WHERE status IN ('completed', 'failed', 'cancelled');
CREATE INDEX IF NOT EXISTS algen_agent_runtime_analytical_graphs_terminal_idx
    ON algen_agent_runtime_analytical_graphs (updated_at)
    WHERE status IN ('completed', 'failed', 'cancelled');
CREATE INDEX IF NOT EXISTS algen_agent_runtime_work_items_finished_idx
    ON algen_agent_runtime_work_items (updated_at)
    WHERE status IN ('completed', 'failed');
