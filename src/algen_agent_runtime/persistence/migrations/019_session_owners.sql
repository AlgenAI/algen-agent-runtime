-- Session ownership: the user each session's memory belongs to.
--
-- Session memory is keyed by (tenant, session), so a run that named another user's session_id
-- read that user's history. The Runtime now claims a session for the run's user before it reads
-- or writes its memory, and refuses a run whose session another user of the tenant owns.
--
-- A claim expires with the memory's retention and is renewed by each claim of its owner. A NULL
-- expires_at never expires: the backfill below uses it for sessions whose memory never expires.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_session_owners (
    tenant_id text NOT NULL,
    session_id text NOT NULL,
    user_id text NOT NULL,
    expires_at timestamptz,
    claimed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, session_id)
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_session_owners_expiry_idx
    ON algen_agent_runtime_session_owners (expires_at)
    WHERE expires_at IS NOT NULL;

-- Existing sessions with memory belong to the user of their earliest run, so a session that was
-- in use before the upgrade cannot be claimed by another user afterwards. A session whose runs
-- were already removed by retention gets no owner: its next run claims it.
INSERT INTO algen_agent_runtime_session_owners (tenant_id, session_id, user_id, expires_at)
SELECT DISTINCT ON (r.tenant_id, r.state->>'session_id')
    r.tenant_id,
    r.state->>'session_id',
    r.state->'request'->>'user_id',
    m.expires_at
FROM algen_agent_runtime_runs r
JOIN (
    SELECT tenant_id, session_id,
        CASE WHEN bool_or(expires_at IS NULL) THEN NULL ELSE max(expires_at) END AS expires_at
    FROM algen_agent_runtime_memory
    GROUP BY tenant_id, session_id
) m ON m.tenant_id = r.tenant_id AND m.session_id = r.state->>'session_id'
WHERE r.state->'request'->>'user_id' IS NOT NULL
ORDER BY r.tenant_id, r.state->>'session_id', r.created_at, r.id
ON CONFLICT (tenant_id, session_id) DO NOTHING;
