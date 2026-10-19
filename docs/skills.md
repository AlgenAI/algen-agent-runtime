# Progressive skill loading

Runtime skills package procedural guidance for a configured agent. A model first sees a compact
catalog of names, versions, and descriptions. It loads detailed guidance only when relevant to the
current task. This reduces repeated prompt content without requiring a framework-specific skill
system or another model call.

## Configure skills on an agent

Add these fields to an agent manifest:

```yaml
enabled_tools: [core.skills_load]
tool_permissions: [skills.read, reports.read]
context_policy:
  skills_enabled: true
  skill_catalog_max_tokens: 2048
  loaded_skills_max_tokens: 4096
skills:
  - name: reporting
    version: 1.0.0
    description: Prepare a report from verified evidence.
    required_permissions: [reports.read]
    instructions: |
      Identify the question and required evidence.
      Check source completeness before comparing totals.
      Explain the findings and list any unresolved discrepancies.
```

`SkillDefinition` is also available from `algen_agent_runtime.types.contracts` for Python-defined
agents. Definitions are explicit entries in `AgentDefinition.skills`; there is no global skill
search, automatic filesystem discovery, network fetch, script execution, or installation. An
application may package and review its definitions before registering the agent. These runtime
entries are separate from this repository's coding-agent `SKILL.md` files.

The feature is disabled by default. Enable the flag, reserved `core.skills_load` tool, and
`skills.read` permission together. Only built-in default and retrieval context builders support it.
An agent may define up to 32 skills, with unique names. Names use lowercase letters, digits,
underscores, periods, or hyphens and begin with a letter (up to 64 characters). Versions follow the
agent-version format. Descriptions allow 1–512 characters and instructions allow 1–16,384 characters.

The catalog includes only skills whose `required_permissions` are already held by the agent.
A skill never grants these permissions or changes `enabled_tools`. Each eventual tool call still
passes the usual tool, authorization, and approval checks. Skill descriptions and bodies should
contain no secrets or credentials.

## Select, replace, and clear

The model calls `core.skills_load` with the complete desired selection:

```json
{"names": ["reporting"]}
```

A selection may contain up to four distinct names. Loading replaces the previous selection;
include all skills needed together. Use `{"names": []}` to clear it. Multiple calls execute in the
runtime's normal tool order; the last accepted selection becomes active. No inference or automatic
selection is performed by Runtime.

The tool receipt lists accepted names and versions. Detailed bodies appear once in a separate
`runtime_loaded_skills` assistant message on the next context build, alongside the
`runtime_skill_catalog` message. Guidance is explicitly subordinate to system instructions, the
user's request, and runtime policy. It is reference material, not a new source of authorization.

Loading is classified as a read: it retrieves configured guidance and changes the run's active
context, without executing skill instructions itself. Normal tool policies and any custom approval
rules apply. Tool output passes `after_tool`, and the accepted selection passes
`before_memory_write` with `operation: skills_load` before checkpointing. Policies may redact or
transform instruction text within its bounds, but must preserve requested names, order, versions,
and source hashes. Invalid, denied, or oversized selections leave the previous checkpoint unchanged.

## Context limits and recovery

The catalog has its own estimated-token limit (default 2,048, range 128–8,192). The combined loaded
instruction block has a separate limit (default 4,096, range 256–16,384). Both limits include their
serialized message wrappers and metadata. They use Runtime's text estimate rather than a provider
specific tokenizer. Oversized catalogs fail explicitly; Runtime does not silently hide accessible
skills. Oversized selections fail without truncating instructions or partially replacing a selection.

Both messages are protected during builder and model-profile packing. Other essential content must
still fit alongside them; budget exhaustion fails explicitly. Clear unused skills or select fewer
skills to free context space. Retrieval evidence competes for the remaining space. History
compaction does not rewrite the active guidance.

`RunState.loaded_skills` checkpoints the accepted instruction text plus names, versions, and hashes
of the original definitions. The hash covers the original description, instructions, version,
name, and sorted permissions. It identifies the configuration source; transformed text may differ
after policy processing. It is not a digital signature or a trust certificate.

Every context build, execution recovery with loaded skills, and completed tool replay checks the
current agent's skill allowlist, permissions, and source identity. Removing or changing a loaded
skill causes the run to fail closed before it can continue using that guidance. Deploy the same
versioned definitions to workers resuming a run; start a new run when changing its skill definitions.
Direct loader calls and replays also verify the originating run, tenant, and user. Loading another
agent's skill by name is not supported, and keyed loads do not use the shared result cache.

The selected bodies and completed-call marker persist in the same run checkpoint before the
`context.skills.updated` event is published. Its payload contains only a count; the corresponding
audit record adds the step ID. If a tool result was recorded before an interrupted checkpoint write,
recovery can validate and apply that result. A committed selection is not reapplied during normal
recovery. Store durability and retention follow the configured run store.

Catalogs and loaded guidance are not appended to canonical execution history or compaction archives.
Only selection arguments and small receipts are retained there; processed instruction bodies remain
in tool-execution records and the current checkpoint. Earlier model answers may of course refer to
skill guidance: clearing a selection is not data erasure. Explicit content-telemetry settings still
apply to ordinary model and tool traces.

Old checkpoints default to an empty selection. Upgrade all strict readers and workers together;
older readers reject the new field. No SQL migration is required. New runs start with an empty
selection even when they share a session. This feature does not provide cross-run learned skills.

Run the offline example:

```bash
python -m examples.progressive_skills
```

See also [structured working state](context-management.md),
[history compaction](artifacts.md#recoverable-history-compaction), and
[model context budgets](providers.md#model-context-budgets).
