DO $$
DECLARE
    old_name text;
    new_name text;
BEGIN
    FOR old_name, new_name IN
        SELECT * FROM (VALUES
            ('traccia_runtime_runs', 'algen_agent_runtime_runs'),
            ('traccia_runtime_events', 'algen_agent_runtime_events'),
            ('traccia_runtime_audit_events', 'algen_agent_runtime_audit_events'),
            ('traccia_runtime_approvals', 'algen_agent_runtime_approvals'),
            ('traccia_runtime_memory', 'algen_agent_runtime_memory'),
            ('traccia_runtime_artifacts', 'algen_agent_runtime_artifacts'),
            ('traccia_runtime_tool_executions', 'algen_agent_runtime_tool_executions'),
            ('traccia_runtime_conversations', 'algen_agent_runtime_conversations'),
            ('traccia_runtime_conversation_messages', 'algen_agent_runtime_conversation_messages'),
            ('traccia_runtime_conversation_events', 'algen_agent_runtime_conversation_events'),
            ('traccia_runtime_analytical_graphs', 'algen_agent_runtime_analytical_graphs'),
            ('traccia_runtime_work_items', 'algen_agent_runtime_work_items'),
            ('traccia_runtime_conversation_feedback', 'algen_agent_runtime_conversation_feedback'),
            ('traccia_runtime_documents', 'algen_agent_runtime_documents')
        ) AS renamed_tables(old_name, new_name)
    LOOP
        IF to_regclass(old_name) IS NOT NULL AND to_regclass(new_name) IS NULL THEN
            EXECUTE format('ALTER TABLE %I RENAME TO %I', old_name, new_name);
        END IF;
    END LOOP;
END $$;

DO $$
DECLARE
    old_name text;
    new_name text;
BEGIN
    FOR old_name, new_name IN
        SELECT * FROM (VALUES
            ('traccia_runtime_runs_tenant_idx', 'algen_agent_runtime_runs_tenant_idx'),
            ('traccia_runtime_audit_tenant_idx', 'algen_agent_runtime_audit_tenant_idx'),
            ('traccia_runtime_approvals_tenant_idx', 'algen_agent_runtime_approvals_tenant_idx'),
            ('traccia_runtime_memory_session_idx', 'algen_agent_runtime_memory_session_idx'),
            ('traccia_runtime_artifacts_tenant_idx', 'algen_agent_runtime_artifacts_tenant_idx'),
            ('traccia_runtime_tool_executions_run_idx', 'algen_agent_runtime_tool_executions_run_idx'),
            ('traccia_runtime_conversations_user_idx', 'algen_agent_runtime_conversations_user_idx'),
            ('traccia_runtime_conversation_messages_idx', 'algen_agent_runtime_conversation_messages_idx'),
            ('traccia_runtime_analytical_graphs_tenant_idx', 'algen_agent_runtime_analytical_graphs_tenant_idx'),
            ('traccia_runtime_work_items_claim_idx', 'algen_agent_runtime_work_items_claim_idx'),
            ('traccia_runtime_conversation_feedback_conversation_idx', 'algen_agent_runtime_conversation_feedback_conversation_idx')
        ) AS renamed_indexes(old_name, new_name)
    LOOP
        IF to_regclass(old_name) IS NOT NULL AND to_regclass(new_name) IS NULL THEN
            EXECUTE format('ALTER INDEX %I RENAME TO %I', old_name, new_name);
        END IF;
    END LOOP;
END $$;
