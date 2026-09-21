# Runtime change checklist

Use the applicable sections; do not force irrelevant work into a small change.

## Boundary and compatibility

- Is this a reusable Runtime capability rather than Studio or marketplace behavior?
- Does it change a public import, YAML contract, event, error code, or persisted schema?
- If it changes `WorkflowManifest`, are manifest validation, execution validation, Studio
  consumption, examples, agent-builder guidance, and the changelog updated together?
- If compatibility changes, is there an upgrade note, migration path, and changelog entry?
- For durable workflows, are tenant isolation, optimistic version conflicts, manifest fingerprints,
  pause/resume, deadline suspension, recovery opt-in, and interrupted side effects tested?
- For workflow approvals, are authenticated reviewer identity, approve/modify/reject outcomes,
  parameter-schema validation, expiry, rejection propagation, checkpoint durability, event-content
  minimization, and invalid-decision deadline behavior tested?
- For child workflows, are exact-version registry resolution, typed input/canonical output,
  correlation and lineage, cycle/depth limits, nested pause/decision propagation, checkpoint-first
  dispatch, bounded parent concurrency, pre-child-creation failure, and recovery without duplicate
  children tested?
- Does core remain usable without a new optional integration installed?
- For artifact changes, are staged pre-run inputs, checksums, lifecycle state, expiry, tenant-scoped
  list/read/delete, PostgreSQL migration, and large-object boundaries covered?
- For object storage, are partial failure compensation, retry-safe deletion, encryption, checksum
  verification, opaque tenant keys, credential discovery, scanner compare-and-set, and audit evidence
  covered without requiring the optional SDK in core installs?
- For email changes, are header injection, recipient and attachment bounds, BCC privacy, TLS,
  artifact-only attachments, stable idempotency, content-minimal receipts/errors, provider refusal,
  and ambiguous delivery failure tested?

## Security and operations

- Preserve tenant isolation and authorization at every read and write boundary.
- Resolve secrets through `env://` or `secret://`; never serialize values into manifests or events.
- Classify tool side effects and idempotency accurately.
- Default interrupted workflow nodes to fail closed; never infer retry safety from node kind alone.
- Bound retries, repairs, loops, fan-out, payload sizes, concurrency, and network access.
- Avoid telemetry content by default; redact credentials and sensitive payloads.
- Keep workflow approval review paths and parameters explicitly secret-free; approval events must not
  expose review values, parameters, or operator comments.

## Evidence

- Unit tests: deterministic contracts and failure behavior.
- Contract tests: provider, tool, store, retrieval, or framework compatibility.
- Integration tests: several Runtime components working together.
- End-to-end tests: public API or representative application path.
- Paid or networked checks use the `live` marker and are never required for ordinary pull requests.

## Quality gates

```bash
ruff format --check src tests examples
ruff check src tests examples
mypy src/algen_agent_runtime
pytest -q -p no:cacheprovider
```

Use `make lint`, `make typecheck`, and `make test` when the Makefile is the better local entry point.
