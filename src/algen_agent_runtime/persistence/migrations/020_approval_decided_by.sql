-- Who decided a run's approval, when, and the note they gave.
--
-- decided_by: the approver's user id (the authenticated principal for POST /v1/runs/{id}/approval
--   and the conversation's owner for a conversation turn), as workflow approvals record it.
-- decided_at: when the decision was recorded.
-- comment: the approver's note, or the reason given for a rejection.
--
-- Columns, not fields in the approval JSON, like those of 017: earlier releases read that JSON
-- with extra="forbid", so rows written by this release stay readable by them during a rolling
-- deploy or after a rollback. Rows decided before this migration, by an earlier release, or by a
-- caller that named no approver have NULL in each. Forward-only and additive.
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS decided_by text;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS decided_at timestamptz;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS comment text;
