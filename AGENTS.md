# Agent Instructions for Algen Agent Runtime

When writing code, tests, documentation, or Git commits in `algen-agent-runtime`, follow these mandatory rules:

## 1. Developer Certificate of Origin (DCO) Signoff
- Every commit in this repository must have a valid `Signed-off-by:` trailer matching the author's git name and email.
- Always use `git commit -s` (or `git commit --signoff`).
- If commits are authored without signoff, rebase them with `git rebase --signoff <base-sha>` before pushing.

## 2. Optional Dependencies & Test Isolation
- **Base Environment**: The core CI jobs (`python`, `coverage`) install only `python -m pip install -e '.[dev]'`. Optional extras (`mcp`, `postgres`, `redis`, `frameworks`, `traccia`, `transformers`, etc.) are **not** present in this base environment.
- **Import Guarding**: Tests that require optional dependencies must be guarded with `pytest.importorskip("<package>")` or conditional skips. They must never raise `ModuleNotFoundError` during test collection.
- **Deterministic Error Testing**: When testing that an adapter or client raises an actionable installation error when an extra is missing, never assume the package is uninstalled in the host environment. Always use `monkeypatch.setitem(sys.modules, "<package>", None)`.
- **Secret Resolution Order**: In adapters and client managers (e.g. MCP transports, external model providers), always validate and resolve configuration and secret references (`env://...`) **before** importing optional packages.

## 3. PostgreSQL & Redis Persistence Rules
- **Relational Constraints**:
  - `algen_agent_runtime_runs` has `id text PRIMARY KEY`. Run IDs are globally unique identifiers; never attempt to create multiple runs with the same `id` across tenants.
  - `algen_agent_runtime_tool_executions` enforces a foreign key constraint referencing `algen_agent_runtime_runs(id)`. Ensure parent runs exist before writing tool execution records.
- **Type Decoding**: `asyncpg` returns raw JSON strings for `jsonb` columns. Deserialize with `json.loads` if the value is a string before validating with Pydantic.
- **Exception Normalization**: Wrap driver-level exceptions such as `asyncpg.exceptions.UniqueViolationError` into runtime-level errors (`algen_agent_runtime.exceptions.errors.ConflictError`).
- **Container CI Testing**: The `container-integration` job requires installing `.[dev,postgres,redis]`.

## 4. Secret Scanning & Security Checks
- Secret detection runs automated scans with open-source Gitleaks (`gitleaks detect --verbose --redact`).
- Never commit literal API keys, bearer tokens, or secrets. Always use `env://` secret references.
