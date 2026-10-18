# Structured working state

An agent can keep a compact record of its goal, constraints, decisions, completed work, and open
tasks while executing a run. This record is independent of conversation summaries: packing and
history compaction do not rewrite it. It is useful for multi-step agents that need to remember
what remains to be done. Simple agents keep the existing behavior unless explicitly enabled.

## Configuration

```yaml
enabled_tools: [core.working_state_update]
tool_permissions: [context.write]
context_policy:
  working_state_enabled: true
  working_state_max_tokens: 2048
```

The runtime registers the reserved `core.working_state_update` tool. Each agent must opt in with
all three settings: the feature flag, enabled tool, and write permission. The default and retrieval
builders support this feature; other builders are rejected when the flag is enabled. No artifact
store, additional model call, or optional dependency is needed.

Updates are classified as writes. The default `approval_policy.require_for_side_effects: true`
therefore requires approval before an update. Applications may use their existing approval policy
to allow these writes; setting that flag to false affects all non-destructive side-effecting tools
for the agent, not just working state. Permission and policy checks still apply.

## Updating the record

The context includes the current record and its revision, beginning with an empty revision 0.
Agents choose when to call the update tool; Runtime does not infer or extract notes automatically.
For example, the tool arguments for the first update are:

```json
{
  "expected_revision": 0,
  "state": {
    "goal": "Complete the comparison report",
    "constraints": ["Use verified source data"],
    "decisions": ["Compare the two proposed options"],
    "completed_work": ["Collected source data"],
    "open_tasks": ["Check totals", "Write the recommendation"]
  }
}
```

Every update replaces the full record. All five fields are required; empty lists deliberately
clear their corresponding entries. The goal allows up to 2,048 characters. Each list allows at
most 16 entries, each containing 1–512 characters. The complete projected message must also fit
`working_state_max_tokens` (default 2,048, configurable from 128 to 8,192). This is the same
serialized-message estimate used by context packing, not an exact provider tokenizer count.

A successful update returns `{"applied": true, "revision": 1}`. A stale revision returns
`{"applied": false, "revision": 1, "reason": "stale_revision"}` without changing the record.
The agent can rebase its next update on the latest context. This also prevents two proposals made
against the same revision from overwriting each other. Invalid, denied, or oversized updates fail
explicitly and leave the working-state checkpoint unchanged.

Normal tool policies process the proposal first. The `before_memory_write` policy then receives
the five-field record with `operation: working_state`; it may redact or transform values while
preserving the schema and size bounds. Policies cannot change the revision precondition.
The checkpoint stores the accepted record, completed-call marker, and small tool receipt together.
A recorded tool proposal can be replayed after an interruption before that checkpoint. A committed
update is not applied again during normal checkpoint recovery.

## Context, persistence, and trust

The built-in builders insert one `runtime_working_state` assistant message containing the current
record. It is labelled untrusted agent-maintained notes, not instructions or authorization. It does
not grant permissions, certify that a task was completed, or override the latest user request.
Applications remain responsible for factual verification of the notes.

The projection is protected by builder packing and model-profile packing, just like the compaction
summary. If required context cannot fit, Runtime raises a budget error instead of dropping the
record. Retrieval continues to select optional evidence from the remaining space. Keep working
state concise enough to leave room for user input, recent exchanges, tools, and model output.

Generated working-state projections are not appended to canonical history or to compaction
archives. The original model tool call still records the proposed update in execution history;
normal history retention, packing, compaction, and content policies apply to those calls.
Processed proposals also remain in tool-execution records. Tool receipts are small and are not
artifact-offloaded. The dedicated `context.working_state.updated` event contains only the committed
revision; its audit record includes the revision and step ID, without note content. Existing
explicit content-telemetry settings still apply to ordinary model and tool traces.

`RunState.working_state` is optional and persists in the existing run checkpoint. An older checkpoint
without the field starts with no record. Upgrade all workers/readers together: older strict readers
reject the new field. No SQL migration is needed. Durability and retention follow the configured
run store; the in-memory store cannot survive process restarts.

The record belongs to its run, whose checkpoint reads are tenant-scoped. There is no target run,
tenant, or user argument on the update tool. A new run starts empty, including runs sharing a
session. Disabling session memory does not disable this explicitly enabled run-local record.
This feature does not add cross-run learning or automatic handoff between agents.

Run the deterministic offline example:

```bash
python -m examples.working_state
```

For related capabilities, see [progressive skills](skills.md), [history compaction and offloading](artifacts.md),
[context packing](architecture.md#context-assembly-and-packing), and
[model context budgets](providers.md#model-context-budgets).
