-- Operator reconciliation and compensation of recorded tool calls.
--
-- New ledger statuses: compensating, compensated and compensation_failed. The status column is
-- free text, so the constraint below names every status the Runtime writes. It is added NOT VALID:
-- it holds for every new write without scanning existing rows. Run
--   ALTER TABLE algen_agent_runtime_tool_executions
--       VALIDATE CONSTRAINT algen_agent_runtime_tool_executions_status_check;
-- afterwards to check older rows as well.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'algen_agent_runtime_tool_executions_status_check'
    ) THEN
        ALTER TABLE algen_agent_runtime_tool_executions
            ADD CONSTRAINT algen_agent_runtime_tool_executions_status_check
            CHECK (status IN (
                'started', 'completed', 'failed', 'indeterminate',
                'compensating', 'compensated', 'compensation_failed'
            )) NOT VALID;
    END IF;
END $$;

-- Listing a tenant's unresolved calls by status and age.
CREATE INDEX IF NOT EXISTS algen_agent_runtime_tool_executions_status_idx
    ON algen_agent_runtime_tool_executions (tenant_id, status, updated_at);
