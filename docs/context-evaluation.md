# Context evaluations and diagnostics

Runtime provides deterministic checks for the context supplied to an agent. Use them to catch
regressions in retention, packing, recovery, and checkpoint size, and to evaluate summaries produced
by your own model. They do not judge factual correctness or instruction-following semantically.

## Offline regression gate

Run the credential-free example from a repository checkout:

```bash
python -m examples.context_evaluation
```

It runs four isolated scenarios through Runtime with scripted model responses:

| Scenario | Expectations |
| --- | --- |
| Simple agent | System instructions and current user input survive within the message budget |
| Compacted history | An older warranty fact remains in the active summary and all source messages can be read back |
| Archive-backed history | The same retention/recovery checks pass with a smaller serialized checkpoint |
| Working state and skills | Checkpoint restoration retains task progress and selected guidance; unselected instructions stay absent |

The command prints a JSON report and exits with status 1 if any case fails. Its integration test also
removes a required fact from a summary and verifies that the gate fails despite successful source
recovery. These fixtures validate Runtime mechanics; their scripted summaries are not evidence of
quality for a deployed model. Existing unit/contract tests additionally exercise artifact permissions,
learned-memory isolation/lifecycle, compaction failures, budget limits, and checkpoint replay.

## Application-owned evaluation cases

Import `ContextEvaluationCase`, `ContextEvaluationArtifact`, and `ContextEvaluationRunner` from
`algen_agent_runtime.evaluation`. The analytical `EvaluationRunner` retains its existing contract.

```python
from algen_agent_runtime.evaluation import (
    ContextEvaluationArtifact,
    ContextEvaluationCase,
    ContextEvaluationRunner,
)
from algen_agent_runtime.types.contracts import Message, Role

case = ContextEvaluationCase(
    id="warranty-retention",
    required_messages=(Message.text(Role.USER, "Keep the warranty unchanged."),),
    required_text=("eighteen months",),
    forbidden_text=("lifetime warranty",),
    max_estimated_tokens=2000,
    max_checkpoint_bytes=16000,
)

# Supply actual observations from your run/provider adapter, without modifying them
# to match the expectations. Route model calls through Runtime's normal governance.
async def execute(case):
    checkpoint, dispatched_request = await run_application_case(case.id)
    return ContextEvaluationArtifact(
        messages=dispatched_request.messages,
        checkpoint_bytes=len(checkpoint.model_dump_json().encode("utf-8")),
    )

report = await ContextEvaluationRunner().run((case,), execute)
assert report.promoted
```

`run_application_case` is an application-supplied executor; the library does not call a provider,
load a dataset, or choose tenant/permission context automatically. The offline example supplies
a complete implementation. Use a captured dispatched request to evaluate the post-router view,
or a rebuilt context to evaluate checkpoint restoration, and record which stage your dataset uses.

Checks have these meanings:

- `required_message.N`: an exactly equal message exists, including role and content. Repeated
  expectations require repeated occurrences. This is not an ordering assertion.
- `required_text.N` / `forbidden_text.N`: case-insensitive substring checks over joined message
  text. They do not infer entailment, distinguish quotations from claims, or inspect non-text blocks.
- `token_budget`: serialized-message estimates fit the bound. Tools, response schemas, model output,
  and provider tokenization are outside this check; router budgets still apply independently.
- `checkpoint_budget`: the supplied UTF-8 byte count fits. Missing measurements fail the check.
- `source_recovery`: `recovered_messages` exactly matches `expected_recovered_messages`, including
  order and duplicates. Missing recovery observations fail. The host must use authorized archive
  reads; an evaluation never overrides retention, lifecycle, tenant, run, or user checks.
- `tool_protocol`: each assistant tool-call group has exactly one result per distinct call ID.
  Orphan, incomplete, duplicate, or non-assistant tool calls fail. Evaluate model-ready views,
  rather than a checkpoint intentionally paused halfway through tool execution.

Each case must declare at least one expectation. IDs must be unique and contain only letters,
digits, dots, underscores, or hyphens, up to 64 characters. Report failures contain check names and
indices, never snippets, messages, or exception payloads. Use non-sensitive dataset identifiers.
Case scores are the fraction of checks passing; the report averages case scores. Promotion requires
every case to pass, as well as meeting the configured threshold (default 1.0). Empty suites never
promote. Executor exceptions produce an `execution` failure and the suite continues; cancellation
propagates. Promotion is only a report field and performs no publishing or deployment.

For deployment evaluations, include long tool exchanges, early constraints, conflicting facts,
corrected preferences, summaries that omit or invent facts, retrieval-heavy runs, and interrupted
checkpoints. Add a domain-specific factual judge or human review for semantic quality. Keep paid or
networked evaluations separate from base CI and explicitly authorize their data/model access.

## Content-free Runtime diagnostics

`context.retrieved` retains its existing fields and adds a post-builder, post-policy snapshot:

| Field | Meaning |
| --- | --- |
| `estimated_tokens` | Total serialized-message estimate before final router packing |
| `retained_history_messages` | Inline canonical/tail message count; null when a custom builder does not initialize history |
| `archived_history_messages` | Absolute history offset removed from inline checkpoints |
| `summary_tokens` | Estimate for historical-summary assistant blocks |
| `working_state_tokens` | Estimate for working-state assistant blocks |
| `skill_catalog_tokens` | Estimate for skill-description assistant blocks |
| `loaded_skills_tokens` | Estimate for selected-guidance assistant blocks |

These are measurements of the current view, not limits or provider usage. An archived count is not
a claim that expired/deleted sources remain available. Named-block estimates include the block's
labels and serialization overhead. `context.compacted` adds `history_tokens_before` and
`history_tokens_after`, measured over projected history (excluding separate skills/working state).
It is emitted only after the compaction checkpoint succeeds.

For agents with compaction enabled, `context.compaction.assessed` explains each planning attempt.
Its data contains `reason`, `estimated_context_tokens`, `trigger_tokens`, `max_input_tokens`,
`planned_message_count`, and `history_offset`. Reasons are:

| Reason | Interpretation / action |
| --- | --- |
| `ready` | A bounded prefix was selected; look for `context.compacted` to confirm persistence |
| `below_trigger` | Current context does not need compaction |
| `recent_groups_only` | The configured recent-group floor leaves no eligible prefix |
| `pending_tools` | Complete pending calls before compacting |
| `incomplete_tool_group` | The next eligible exchange lacks results; inspect execution/recovery |
| `input_limit` | The first eligible source cannot fit the summary input allowance; reduce payloads/offload or increase the allowance |
| `no_source` | Eligible messages contain only system instructions, which are preserved verbatim |

`assess_compaction` in `context.compaction` also reports `disabled` and `history_uninitialized` to
direct callers. Runtime initializes history before planning and emits no assessment for agents with
compaction disabled. `plan_compaction` remains available for callers needing only a plan.
Pending-tool assessments report configured limits; other Runtime assessments use effective adaptive
limits. A ready assessment can precede policy denial, model failure, or archive failure. It is an
observation, not a durable commit receipt; retry/recovery may produce another assessment.

The new diagnostic fields and events include only counts, estimates, and fixed reason codes, even
when content telemetry is enabled. Existing opt-in citation/model-content telemetry is unchanged.
Upgrade event consumers together: older strict `EventType` readers do not recognize
`context.compaction.assessed`. No checkpoint or SQL schema migration is required for these diagnostics.
