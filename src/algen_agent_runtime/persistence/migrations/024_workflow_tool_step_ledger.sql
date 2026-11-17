-- A workflow tool step (handler: tool:<name>) records its call in the tool-execution ledger under
-- the workflow run's id, which is not an agent run. The foreign key from the ledger to
-- algen_agent_runtime_runs refused those records, so every tool step failed on PostgreSQL with
-- ForeignKeyViolationError. Drop it: a ledger record's run_id is the agent run or workflow run
-- that made the call. Terminal-run retention deletes an agent run's records with it, and
-- workflow checkpoint retention a workflow run's. Dropping a constraint takes a brief lock and
-- scans nothing.
DO $$
DECLARE
    ledger regclass := to_regclass('algen_agent_runtime_tool_executions');
    runs regclass := to_regclass('algen_agent_runtime_runs');
    constraint_name text;
BEGIN
    IF ledger IS NULL OR runs IS NULL THEN
        RETURN;
    END IF;
    FOR constraint_name IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = ledger AND confrelid = runs AND contype = 'f'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', ledger, constraint_name);
    END LOOP;
END $$;
