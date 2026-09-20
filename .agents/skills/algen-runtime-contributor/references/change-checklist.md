# Runtime change checklist

Use the applicable sections; do not force irrelevant work into a small change.

## Boundary and compatibility

- Is this a reusable Runtime capability rather than Studio or marketplace behavior?
- Does it change a public import, YAML contract, event, error code, or persisted schema?
- If compatibility changes, is there an upgrade note, migration path, and changelog entry?
- Does core remain usable without a new optional integration installed?

## Security and operations

- Preserve tenant isolation and authorization at every read and write boundary.
- Resolve secrets through `env://` or `secret://`; never serialize values into manifests or events.
- Classify tool side effects and idempotency accurately.
- Bound retries, repairs, loops, fan-out, payload sizes, concurrency, and network access.
- Avoid telemetry content by default; redact credentials and sensitive payloads.

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

