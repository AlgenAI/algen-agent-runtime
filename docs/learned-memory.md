# Durable learned memory

Learned memory retains explicitly selected facts, preferences, and procedures across runs. It is
separate from session transcripts, working state, history summaries, and configured skills. Agents
choose what to remember and when to recall it through governed tools; Runtime does not silently
extract or promote conversation content into long-term memory.

## Enable storage and tools

For durable storage, install the existing `postgres` extra and configure PostgreSQL:

```yaml
storage:
  learned_memory_store: postgres
  postgres_dsn: env://ALGEN_POSTGRES_DSN
```

The default `learned_memory_store: memory` adapter is ephemeral. It preserves records only for the
life of that store instance. PostgreSQL migration `011_learned_memory.sql` adds the learned-record
and operation-receipt tables. It runs through normal schema initialization. Use the migration before
starting workers when automatic schema initialization is disabled. No Redis adapter is provided for
this memory layer.

Add these fields to an agent definition:

```yaml
learned_memory_policy:
  enabled: true
  max_recall_tokens: 2048
  max_retention_seconds: 2592000
enabled_tools: [core.memory_recall, core.memory_write, core.memory_delete]
tool_permissions: [memory.read, memory.write, memory.delete]
```

All features are disabled by default. The container registers the three reserved tools; each agent
must explicitly enable the subset it needs and grant the corresponding permissions. A read-only
agent can enable only `core.memory_recall`. Direct `AgentRuntime` construction accepts a
`learned_memory_store` implementing `algen_agent_runtime.memory.contracts.LearnedMemoryStore`.
It registers memory tools only when that store is provided. No custom context builder support is
needed because recall uses ordinary governed tool results.

Writes are `SideEffect.WRITE`; deletes are `SideEffect.DESTRUCTIVE`. Default approval policies pause
both operations before they can change the store. Read operations use normal read/tool policies.
Setting `approval_policy.require_for_side_effects: false` affects all non-destructive writes for that
agent; it does not turn off the separate destructive-action approval setting. Session
`memory_policy.enabled` does not control this separately enabled memory layer.

## Remember, recall, correct, and delete

A first write uses revision 0:

```json
{
  "key": "report-format",
  "expected_revision": 0,
  "kind": "preference",
  "content": "Use concise tables for comparison reports.",
  "evidence": "The user explicitly requested concise comparison tables.",
  "retention_seconds": 86400
}
```

Call `core.memory_write` with this object. The receipt contains the key, new revision, and deletion
status. Keys begin with a lowercase letter and contain up to 64 lowercase letters, digits, periods,
underscores, or hyphens. Kinds are `fact`, `preference`, and `procedure`. Content allows 1–2,048
characters and evidence allows 1–1,024. Evidence is the agent's source explanation, not proof or an
independently verified citation. Runtime records the source run, tool step, and agent version itself.

`core.memory_recall` supports either an exact key or a case-insensitive literal substring query:

```json
{"key": "report-format"}
```

```json
{"query": "comparison", "limit": 5}
```

An empty query lists recent active records. By default (`recall: substring`) search matches keys and
content literally, and results are ordered by the source-run timestamp and key. An agent can rank
by meaning instead; see [Similarity-ranked recall](#similarity-ranked-recall). The default limit is 5,
maximum 20. Recall also has a serialized-message token estimate (`max_recall_tokens`, default 2,048,
range 128–8,192). Search considers at most `limit + 1` records; oversized candidates can be omitted,
and `truncated: true` signals a count or context-budget limit. Exact-key recall fails explicitly if
the record cannot fit. It never returns a partial content string as though it were the full record.

To correct a record, recall it and send the complete replacement with its current
`expected_revision`. To delete it, call `core.memory_delete`:

```json
{"key": "report-format", "expected_revision": 1}
```

A successful delete clears the stored content and evidence and increments the revision. Search
excludes deleted and expired records. Exact-key recall returns their content-free metadata, including
the revision needed for an explicit recreation or further deletion. Revision 0 means a key that has
never existed, not a key whose content was deleted or expired. A stale revision raises `ConflictError`
and does not change memory; the run fails explicitly so a new attempt can recall the current value.

## Similarity-ranked recall

A literal match finds a record only when the agent guesses its wording: a query for "invoice
layout" misses a procedure stored as "billing statement format". `learned_memory_policy.recall`
chooses how a non-empty `query` is answered:

| `recall` | Ranking |
| --- | --- |
| `substring` (default) | Literal, case-insensitive match of key and content, newest first. Unchanged behaviour. |
| `semantic` | Cosine similarity between the query's embedding and each record's content embedding. |
| `hybrid` | `(1 - hybrid_keyword_weight) × similarity + hybrid_keyword_weight` when the key or content also contains the query literally. |

```yaml
providers:
  embeddings:
    type: openai_compatible
    base_url: https://api.openai.com/v1
    api_key: env://OPENAI_API_KEY
    default_model: text-embedding-3-small
agents:
  - name: analyst
    learned_memory_policy:
      enabled: true
      recall: hybrid               # substring | semantic | hybrid
      min_score: 0.35              # drop weaker matches (0-1, default 0)
      hybrid_keyword_weight: 0.3   # hybrid only (0-1, default 0.3)
      embedding_provider: embeddings  # a configured provider that implements embed()
      embedding_model: text-embedding-3-small
      embedding_version: "1"       # optional; change it to re-embed every record
```

`semantic` and `hybrid` need `embedding_provider`, which must name a configured provider (settings
validation and run start both check it). Embeddings go through the provider contract's
`embed(texts, model)`, so no new dependency is needed. Exact-key lookups and an empty query behave as
in `substring` mode and call no embedding model.

- **On write**, `core.memory_write` embeds the new content and stores the vector with the record
  revision, in the same transaction. A correction replaces the vector; a deletion removes it. If the
  embedding call fails, the write fails and memory is unchanged.
- **On recall**, Runtime embeds the query and ranks the scope's active records by similarity, then
  applies `min_score` and `limit`. A weak match returns nothing rather than noise. Records keep the
  same shape; the ranking is their order. The `max_recall_tokens` budget, `truncated`, replay
  validation and the tool's governance are unchanged: lower-ranked records are the ones cut.
- **Model identity.** Each vector records the embedding identity (provider, `embedding_model` and
  `embedding_version`). Vectors of another identity are never compared with the query. Records
  without a current vector (written in `substring` mode, before an upgrade, or under another model)
  are embedded during recall, up to 16 per call, in the same provider call as the query; so after
  changing the model, recall re-embeds the most recent records first and ranks the rest once they
  are reached. `hybrid` still finds those by literal match meanwhile.
- **Privacy.** The query and content go only to the configured embedding provider. They are not
  logged, put in spans or used in cache keys by this feature; an embedding failure reports the
  provider error type only. Recall arguments and results follow the normal tool telemetry settings.

Similarity recall needs a store that keeps vectors: both built-in stores do
(`algen_agent_runtime.memory.contracts.SimilarityMemoryStore`). A custom `LearnedMemoryStore` that
does not implement it keeps working in `substring` mode and is rejected at run start for the others.

**In memory**, vectors live in the store's process with the records.

**PostgreSQL**, migration `018_learned_memory_vectors.sql` adds
`algen_agent_runtime_learned_memory_vectors`: one row per record with the revision, embedding
identity, dimensions and the unit vector as `real[]`, with a foreign key to the record (`ON DELETE
CASCADE`). The migration needs no extension, so every deployment can apply it, with or without
pgvector and without the privileges `CREATE EXTENSION` requires. When the `vector` extension is
installed in the database and visible on the search path (with `postgres_schema`, install it in that
schema), ranking uses pgvector's inner-product operator; otherwise it is computed in SQL. Both are
exact: ranking reads only the caller's tenant, user and agent rows through the primary key. No
approximate (HNSW) index is created, because its dimensions depend on the embedding model and a
global approximate search filtered to one scope could miss that scope's records. Learned memory per
scope is small and bounded by the `learned_memory` storage quota; vectors are not counted in it.

Expiry hides a record from ranking immediately, and the maintenance sweep removes its vector with
its content. The sweep also removes vectors a previous release left behind after a rollback (of
records it deleted or corrected).

## Isolation, policy, and recovery

Every record is scoped to `(tenant_id, user_id, agent logical name)`. The scope is taken from the
originating run, not model-supplied tool arguments. Agent versions share that logical agent's memory;
other agents and users do not. There is no tenant-wide sharing, cross-agent handoff, or automatic
conversion of remembered procedures into executable skills. A procedure remains untrusted data.
Recall is on demand, with no automatic injection or extra model call.

Tool permission, approval, and `after_tool` policies process proposals before any write occurs.
`before_memory_write` then receives the proposal with `operation: core.memory_write` or
`operation: core.memory_delete`. Policies may redact or transform content and evidence, while
preserving the key, expected revision, kind, and retention. Invalid or denied proposals cannot alter
the learned store. Recall output also passes normal tool policies and is checked against the current
record revision, lifecycle, execution scope, and budget before it enters the transcript.

The store applies each revision change and its operation receipt atomically. PostgreSQL serializes
updates within a user/agent scope and uses revision checks for competing writes. Operation IDs come
from the run and tool-call identity. Replaying an operation returns its original receipt without
reapplying it, even after a later correction, expiry purge, or deletion. Reusing that operation ID
with different processed content fails. This also covers an interruption after the learned-store
commit but before the run checkpoint: normal recovery consumes the recorded proposal and completes
the run checkpoint without repeating the mutation.

Recall uses keyed executions without shared result caching. Completed-read replay verifies its
binding to the originating execution and rechecks revisions and expiry. If a recalled record has
changed or expired, replay fails instead of silently returning obsolete data. This does not retract
content a model has already received or rewrite earlier transcript messages.

Writes are timestamped from the source run's start, and expiry is anchored to that same timestamp,
so recovery cannot extend retention. The default requested lifetime is one day. An agent may cap it
with `max_retention_seconds` (default 30 days, configurable from 60 seconds to one year). Individual
writes must request between 60 seconds and one year and stay within the agent's cap. Very long paused
runs may need a fresh run if their proposed write has already expired. Keep agent versions and
policies consistent when recovering interrupted writes; changing the processed proposal can cause
an operation-identity conflict.

The dedicated `memory.learned.updated` event follows the run checkpoint and contains only revision
and deletion status. Its audit entry adds the step ID. Normal explicit content telemetry still
applies to model/tool traces. Full write proposals and recalled data can remain in execution records
and canonical history according to their own retention settings.

## Expiry, deletion, and operations

Expired content is immediately hidden from tool recall, even before a maintenance sweep. Maintenance
clears expired content and evidence in bounded batches and reports `purged_learned_memory`.
The normal maintenance CLI/background settings apply. Low-level stores also expose
`purge_expired(limit=1000)`; direct store access is a trusted application interface, not a governed
agent tool.

Content-free revisions and operation receipts are retained to prevent stale retries from resurrecting
memory. They have no automatic garbage collection in this version. Source-run identifiers have no
foreign key to the run table: deleting a source run does not erase long-term memory. Monitor record
and receipt growth and apply the deployment's data-retention requirements deliberately. Deleting a
learned record is not a promise to erase prior transcripts, logs, database backups, or tool results.

This feature adds versioned storage and explicit recall, optionally ranked by similarity, not
automatic consolidation, deduplication, or factual validation. It requires no new core dependencies. PostgreSQL remains
optional; migration and concurrency tests run in the container-integration job.

Run an offline example using the ephemeral adapter:

```bash
python -m algen_agent_runtime_examples.learned_memory
```

See [structured working state](context-management.md), [progressive skills](skills.md), and
[maintenance](operations.md#maintenance-and-data-retention) for related capabilities.
