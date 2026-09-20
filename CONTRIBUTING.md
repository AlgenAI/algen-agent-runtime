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

- `tests/unit`: deterministic behavior with no external services.
- `tests/contract`: provider, tool, and adapter compatibility contracts.
- `tests/integration`: multiple runtime components working together.
- `tests/end_to_end`: public API and representative application paths.

Tests must be tenant-aware, deterministic by default, and safe to run without credentials. Add regression coverage for bug fixes and failure-path coverage for security-sensitive changes.

## Documentation and compatibility

Update documentation and examples when configuration, commands, behavior, or public imports change. Update [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

During the `0.x` series, public contracts are experimental, but breaking changes still require an upgrade note, tests, and a documented rationale. See the [API stability policy](docs/api-stability.md).

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

CI rejects unsigned pull-request commits.

## Conduct and support

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Usage questions and support expectations are described in [SUPPORT.md](SUPPORT.md).
