-- Find a conversation turn's run: a restarted ConversationService looks a streaming turn's run
-- up by (tenant, conversation, turn) when the turn's message has no run id yet (a stop between
-- starting the run and storing its id, or a message stored by an earlier release).
-- Additive: an expression index over the existing run state, only for runs that name a turn.
CREATE INDEX IF NOT EXISTS algen_agent_runtime_runs_turn_idx
    ON algen_agent_runtime_runs (
        tenant_id,
        (state->'request'->>'conversation_id'),
        (state->'request'->>'turn_id')
    )
    WHERE state->'request'->>'turn_id' IS NOT NULL;
