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
- **Local Integration Testing (every change)**: Before committing any change, run the whole suite against real PostgreSQL and Redis, not only the unit tests:
  1. Install the extras once: `python -m pip install --isolated -e '.[dev,postgres,redis]'`.
  2. Run `scripts/integration-tests.sh` (or pass pytest arguments, e.g. `scripts/integration-tests.sh tests/integration -k approvals`). The script checks for `docker` first, then `podman`, and stops if neither is installed. It starts a fresh `postgres:17-alpine` container on port 55432 and a `redis:7-alpine` container on port 56379, waits until both accept connections, sets `POSTGRES_DSN` and `REDIS_URL`, and runs pytest. It then removes both containers, whether the tests pass, fail or are interrupted.
  3. Never point the tests at a long-lived local database such as the server on port 5432. Many tests insert fixed ids (`run-1`, `wf-1`), so a reused database fails with `ConflictError`. Each run needs fresh containers.
  4. Afterwards, check that no containers are left: `podman ps -a --filter name=algen-rt-test` (or `docker ps -a ...`) must be empty.
  5. Report the result in the PR's test plan. PostgreSQL logs `ERROR: duplicate key` and `violates check constraint` lines during a passing run: those come from tests that check conflicts are rejected, and they are expected.

## 4. Secret Scanning & Security Checks
- Secret detection runs automated scans with open-source Gitleaks (`gitleaks detect --verbose --redact`).
- Never commit literal API keys, bearer tokens, or secrets. Always use `env://` secret references.
