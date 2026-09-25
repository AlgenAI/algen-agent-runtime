# Contributing to Algen Agent Runtime

Thank you for helping improve Algen Agent Runtime. Contributions must preserve provider-neutral contracts, tenant boundaries, deterministic tests, and the optional-dependency model.

## Before opening a change

- Search existing issues and pull requests to avoid duplicate work.
- Use an issue for substantial features, public API changes, persisted-schema changes, or security-boundary changes before investing in implementation.
- Report vulnerabilities privately through [SECURITY.md](SECURITY.md), never through a public issue.
- Keep private Studio code, credentials, customer data, proprietary prompts, and internal endpoints out of this repository.

Small bug fixes, tests, and documentation improvements can be submitted directly.

## Development setup

Python 3.12 and 3.13 are supported and tested.

```bash
git clone https://github.com/AlgenAI/algen-agent-runtime.git
cd algen-agent-runtime
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Run the same core checks used by CI:

```bash
ruff format --check src tests examples
ruff check src tests examples
mypy src/algen_agent_runtime
pytest -q -p no:cacheprovider
```

Or use the Make targets:

```bash
make lint
make typecheck
make test
```

Tests requiring paid credentials or external services must use the `live` marker and must not run in untrusted pull-request contexts.

## Repository boundaries

- Keep orchestration and contracts provider-neutral.
- Add reusable runtime capabilities here; keep product UI and project-management concerns in their owning applications.
- Keep provider, framework, database, vector-store, and local-model dependencies optional unless required by the core runtime.
- Treat tenant identifiers, authorization, secret resolution, approvals, tool idempotency, network access, persistence, and telemetry content as security boundaries.
- Preserve stable error codes and add forward migrations for persisted-schema changes.
- Keep Traccia as an optional telemetry integration; runtime orchestration must not depend on Traccia SDK types.
- Do not add Algen Agent Studio code, assets, schemas, settings, tests, or build steps to this repository. Studio consumes Runtime as an installed package.

## Tests

Add the narrowest test that proves the behavior at the appropriate level:

- `tests/unit`: deterministic behavior with no external services (e.g. Redis/in-memory queue behavioral doubles).
- `tests/contract`: provider, tool, and adapter compatibility contracts.
- `tests/integration`: multiple runtime components working together (marked with `integration`, `postgres`, or `redis`).
- `tests/end_to_end`: public API and representative application paths.

### Running Integration & Container Tests
Integration tests for PostgreSQL and Redis run against local doubles or live containers:
```bash
# Install development dependencies along with postgres and redis drivers
python -m pip install -e '.[dev,postgres,redis]'

# Run tests targeting storage integration markers against in-memory doubles
pytest -q -p no:cacheprovider -m "integration or postgres or redis"

# Run tests against live local container instances
POSTGRES_DSN="postgresql://postgres:secret@localhost:5432/algen_test" \
REDIS_URL="redis://localhost:6379/0" \
pytest -q -p no:cacheprovider -m "integration or postgres or redis"
```

When implementing or testing persistence stores:
- **Relational Integrity**: Respect primary key constraints (`id text PRIMARY KEY` for runs is globally unique across tenants) and foreign keys (`algen_agent_runtime_tool_executions` references `algen_agent_runtime_runs(id)`). Mocks must not violate schema constraints.
- **Type Decoding**: Note that database drivers such as `asyncpg` return raw JSON strings for `jsonb` columns; always deserialize them (e.g. `json.loads`) before dictionary casting.
- **Exception Normalization**: Normalize database-specific exceptions (such as `asyncpg.exceptions.UniqueViolationError`) into runtime errors (such as `ConflictError`).

### Optional Dependencies & Test Isolation
Algen Agent Runtime maintains strict isolation of optional extras (`mcp`, `postgres`, `redis`, `frameworks`, etc.):
- **Secret Resolution Order**: In adapters and clients (e.g. MCP transports), always validate and resolve secret references (`env://...`) **before** importing optional packages.
- **Graceful Skipping in Base Tests**: Core test runs install only `.[dev]`. Tests requiring optional packages must guard execution with `pytest.importorskip("<package>")` or conditional skips, never failing test collection.
- **Missing Dependency Testing**: Test missing-extra error messages deterministically using `monkeypatch.setitem(sys.modules, "<package>", None)` rather than assuming the package is missing from the environment.

## Documentation and compatibility

Update documentation and examples when configuration, commands, behavior, or public imports change. Update [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

During the `0.x` series, public contracts are experimental, but breaking changes still require an upgrade note, tests, and a documented rationale. See the [API stability policy](docs/api-stability.md).

## Secret Scanning & Security Checks

Automated CI runs secret detection on all commits using open-source Gitleaks:
```bash
gitleaks detect --verbose --redact
```
Never commit API tokens, keys, passwords, or credentials. Use `env://` secret references in configurations and tests.

## Pull requests

Keep pull requests focused and explain:

- the problem and intended outcome;
- security, privacy, and tenant-isolation impact;
- public API, manifest, dependency, and persisted-data compatibility;
- tests and manual verification performed;
- rollout or rollback considerations where applicable.

All required checks and maintainer reviews must pass before merge. Do not rewrite unrelated files or include generated caches, local databases, build products, or secrets.

## Developer Certificate of Origin

The project uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Sign off every commit to certify that you have the right to submit the contribution under this project's license:

```bash
git commit -s
```

CI rejects unsigned pull-request commits. If you authored commits without `-s`, rebase and sign them before submitting or updating a pull request:

```bash
git rebase --signoff <base-branch-or-sha>
git push --force-with-lease origin <branch-name>
```

## Conduct and support

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Usage questions and support expectations are described in [SUPPORT.md](SUPPORT.md).
