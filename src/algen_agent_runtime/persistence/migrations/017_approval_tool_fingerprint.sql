-- What a tool call's approval covers, why the agent asked for it, and why an approval was
-- expired before its call ran.
--
-- tool, tool_version: the tool the approval was asked for.
-- tool_fingerprint: SHA-256 of the tool's definition (name, version, input and output schema,
--   side-effect class, requires_approval) and its handler's source when available, when the
--   approval was asked for. Before the approved call runs, a different fingerprint expires the
--   approval (status_reason 'tool_changed') and a new one is asked for.
-- arguments_sha256: SHA-256 of the canonical JSON of the arguments the approval proposes.
-- rationale: why the agent wants to make the call (the model's text that accompanied it,
--   redacted like the parameters and capped in length), shown to the approver.
-- status_reason: why the request reached its status, when the status alone does not say.
--
-- Columns, not fields in the approval JSON: 0.1.0a17 reads that JSON with extra="forbid", so rows
-- written by this release stay readable by earlier ones during a rolling deploy or after a
-- rollback. Rows written before this migration have NULL in each: their approved calls run as
-- before, without a fingerprint check. Forward-only and additive.
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS tool text;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS tool_version text;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS tool_fingerprint text;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS arguments_sha256 text;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS rationale text;
ALTER TABLE algen_agent_runtime_approvals ADD COLUMN IF NOT EXISTS status_reason text;
