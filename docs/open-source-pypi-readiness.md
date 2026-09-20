# Open-Source and PyPI Readiness Plan

Last updated: 2026-09-20

## Implementation status — 2026-09-19

The first P0 implementation slice is complete:

- Added `LICENSE`, `NOTICE`, `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`,
  `CODE_OF_CONDUCT.md`, `GOVERNANCE.md`, and `SUPPORT.md`.
- Added CODEOWNERS, issue forms, a pull-request template, and an automated DCO
  sign-off check.
- Added Python 3.12/3.13, build, artifact-inspection, clean-install,
  secret, dependency, static-analysis, and container-scan workflows.
- Expanded PyPI metadata and moved package/API version reporting to the single
  source `algen_agent_runtime.__version__` at prerelease version `0.1.0a1`.
- Constrained source-distribution contents and verified that wheel artifacts
  contain typing metadata, SQL migrations, and licensing files.
- Built and validated the wheel and source distribution locally with Twine.
- Changed the built-in API default to loopback, removed implicit development
  scopes, and required an explicit unsafe override for non-loopback development
  authentication.
- Established a high-severity Bandit release gate. Existing medium-confidence
  dynamic-SQL findings remain open for validation/annotation before public beta.
- Raised the development-test floor to pytest 9.0.3 after the local audit found
  the advisory affecting the previous pytest 8 range; security CI upgrades pip
  before auditing the isolated core environment. Raised pytest-asyncio to the
  1.3+ line that supports pytest 9.
- Added a 75% coverage floor from a measured 78% baseline so coverage can be
  ratcheted upward with security, persistence, and recovery tests.

The repository-facing technical work is complete enough for a public source
review: Runtime and private Studio ownership are separated, community health
files are present, package contents are bounded, and quality, security, build,
and release workflows are defined. Public visibility still requires the
human-owned launch checks below.

## Current release verdict

There are three distinct release decisions:

- **Public source repository:** technically prepared; publish only after the legal/brand, full-history secret, and repository-settings checks below are signed off by the owner.
- **PyPI alpha:** workflow prepared; publish only after the `algen-agent-runtime` project, protected `pypi` environment, and Trusted Publisher are configured and the release candidate passes CI.
- **Production-readiness claim:** not approved. The runtime gaps in admission control, distributed ownership, reconciliation, and tenant-scoped administration remain tracked in `production-readiness.md`.

The repository now has the initial legal text, contribution and governance
policies, security reporting guidance, changelog, quality/security workflows,
artifact validation, and single-source prerelease versioning. The built-in
server authentication default has also been hardened. Publication remains
blocked on human-owned legal/brand/history review, protected release identities,
service-backed CI, and the remaining critical runtime gaps in admission control,
distributed ownership, tool reconciliation, and tenant-scoped agent
administration.

## P0: owner checks before making the repository public

### Legal and brand

- Add the complete Apache License 2.0 text as `LICENSE`; package metadata alone is insufficient.
- Confirm every source file, example asset, dataset, prompt, copied schema, font, image, and dependency is owned or redistributable under compatible terms.
- Add `NOTICE` when required by included material or organizational policy.
- Perform a trademark search for **Algen-Agent-Runtime**, **Algen Agent Runtime**, and **Algen Agent Studio** in target markets.
- Verify and reserve the PyPI distribution, GitHub organization/repository, documentation domain, social handles, and package-signing identities. The automated PyPI page check was inconclusive because PyPI returned a CAPTCHA; verify ownership manually while authenticated.
- Resolve the public identity mismatch between repository name `algen-agent-core` and distribution name `algen-agent-runtime`. Prefer a repository name such as `algen-agent-runtime` unless `algen-agent-core` is intentionally the umbrella project.

### Security and history hygiene

- **Do not make the existing Git history public as-is.** Historical commits contain removed customer-specific schema/context material and personal contributor email addresses. Create the public repository from an audited clean snapshot, or perform an owner-approved history rewrite and verify every ref before changing visibility.
- Scan the full Git history, not only the current tree, for API keys, passwords, connection strings, private URLs, customer names, proprietary datasets, generated artifacts, and personal data.
- Rotate any secret ever committed, even if later deleted.
- Run secret, dependency, static-analysis, and container scans in CI.
- Replace the insecure default of public bind plus caller-supplied development identity before presenting the server as deployable.
- Threat-model plugin loading, HTTP tools, document ingestion, artifact downloads, JWT configuration, SSRF/DNS rebinding, archive export, and multi-tenant storage.
- Add resource admission limits before exposing a hosted demo.
- Review error messages, events, traces, and example dashboards for prompt, PII, SQL, token, and credential leakage.
- Publish a supported-version policy and private vulnerability-reporting channel in `SECURITY.md`.

### Repository governance

- Add `CONTRIBUTING.md` with environment setup, architecture boundaries, style, tests, pull-request expectations, and contributor certificate or CLA policy.
- Add `CODE_OF_CONDUCT.md` using a recognized template and name enforcement contacts.
- Add `GOVERNANCE.md` defining maintainers, decision making, release authority, and succession.
- Add `CODEOWNERS`, issue templates, pull-request template, support boundaries, and a responsible disclosure template.
- Decide whether contributions use Developer Certificate of Origin sign-off or a Contributor License Agreement; automate the chosen check.
- State the relationship between AlgenAI, Traccia, and the package so users understand stewardship and trademarks.

## P0: package correctness

### Metadata

Expand [`pyproject.toml`](../pyproject.toml) with:

- Authors and maintainers.
- Homepage, source, documentation, issue tracker, and changelog URLs.
- PyPI classifiers for development status, audience, license, operating system, Python versions, typing, and topic.
- Keywords aligned with governed agents, agent runtime, AI governance, RAG, and model routing.
- A valid SPDX license expression and license-files declaration supported by the chosen metadata version.
- A single-source dynamic version instead of repeating `0.1.0` in package metadata, [`__init__.py`](../src/algen_agent_runtime/__init__.py), and [`api/app.py`](../src/algen_agent_runtime/api/app.py).

Do not claim OS, Python version, provider, or production support that CI does not exercise.

### Build contents

- Build both wheel and source distribution in a clean environment.
- Inspect archives to ensure `py.typed`, SQL migrations, license, required README content, and package modules are included.
- Ensure tests, local databases, caches, screenshots, secrets, and large example artifacts are excluded unless intentionally distributed.
- Install the wheel into a fresh environment and run import, CLI, migration discovery, FastAPI startup, and one mock-agent smoke test from outside the repository.
- Run `twine check` or the equivalent metadata validation on both artifacts.
- Verify every optional extra independently and in important combinations. Error messages must name the correct distribution extra; adapter messages currently use inconsistent package naming.
- Decide whether examples ship in the wheel. Prefer keeping substantial examples in the repository while publishing small copyable templates or a separate examples package.

### Dependency policy

- Document minimum-version reasoning and supported Python versions.
- Test minimum and latest compatible dependency sets to detect overly broad ranges.
- Generate a release SBOM and dependency/license report.
- Add automated dependency updates and vulnerability alerts.
- Keep heavy framework, vector-store, local-model, and document dependencies optional.
- Review optional-extra resolution together; packages such as Torch, CrewAI, Chroma, and framework SDKs can create large or conflicting environments.

## P0: quality gates

Create CI for pull requests and the protected default branch with:

1. Ruff formatting/checks.
2. Strict mypy.
3. Unit, contract, integration, and end-to-end test groups.
4. Python 3.12 and 3.13 at minimum; add 3.14 only after all dependencies support it.
5. PostgreSQL, Redis, and pgvector-backed integration jobs.
6. Wheel/sdist build and clean-install smoke tests.
7. Documentation link and example validation.
8. Secret, dependency, static security, and container image scanning.
9. Coverage reporting with a ratcheted threshold, emphasizing security and recovery branches rather than a vanity percentage.
10. Deterministic mock-provider tests requiring no paid credentials.

Mark credentialed provider tests separately and run them in a protected scheduled workflow with strict spending limits. Never run forked pull-request code with repository secrets.

## P1: product readiness before broad promotion

### Stabilize the public API

- Define which imports are public. The current root [`algen_agent_runtime.__init__`](../src/algen_agent_runtime/__init__.py) exposes only a small subset while examples import many internal module paths.
- Publish an API stability policy using semantic versioning and explicit deprecation windows.
- Separate protocols/contracts from default implementations where users are expected to extend behavior.
- Add compatibility tests for agent manifests, persisted state, events, plugin API versions, and exported projects.
- Avoid storing opaque Python objects such as framework resume state when durability is claimed.
- Decide whether analytics, conversations, document security, and framework adapters remain modules in one distribution or become companion packages with synchronized compatibility ranges. Studio is maintained as a separate private distribution that depends on the public Runtime package.

### Fix production blockers

Complete the critical items in the [production-readiness roadmap](production-readiness.md):

- Secure authentication defaults.
- Global and per-tenant admission control.
- Distributed run ownership with leases and fencing.
- Tool-execution reconciliation.
- Persistent tenant-scoped agent definitions.

Until these are complete, describe the project as an early-stage single-node runtime foundation, not a production distributed control plane.

### Make the first-use path reliable

- Provide `algen-agent init`, `algen-agent validate`, `algen-agent dev`, `algen-agent run`, and `algen-agent doctor`.
- Make `Container` usable as an async context manager.
- Validate referenced providers, environment variables, tools, permissions, context builders, planners, verifiers, policies, and workflows before accepting a run.
- Add a deterministic mock profile so the quickstart works without credentials or local model installation.
- Guarantee one quickstart from install to successful run in under ten minutes.
- Produce actionable errors with stable codes and configuration paths.

### Curate examples

- Repair or remove examples that reference unregistered tools or inert workflow definitions.
- Give every supported example one setup command, one run command, one test command, and one reset command.
- Add manifests listing required extras, services, environment variables, expected runtime, and mock support.
- Use only synthetic data and make that explicit.
- Test examples against an installed wheel.
- Center the portfolio on customer support, incident response, invoice exceptions, contract review, and governed analytics, with ownership and production limitations stated in each example README.

## P1: documentation required for adoption

Before launch, provide:

- A short value proposition that says **governed agent runtime**, not a generic AI framework.
- A copy-paste quickstart that has been tested in a clean environment.
- Concepts: agent definition, run, tool, policy point, approval, verifier, provider profile, context builder, event, and store.
- Architecture and trust-boundary diagrams.
- Configuration reference generated from Pydantic/JSON Schema where possible.
- Public Python API and HTTP API references.
- Tutorials for a single agent, typed tool, RAG, approval, multi-agent workflow, evaluation, and external-framework integration.
- Production guidance for auth, reverse proxy, TLS, CORS, storage, migrations, backups, retention, telemetry, scaling, and shutdown.
- Security guidance for prompts, documents, tools, SSRF, subprocesses, secrets, PII, tenant isolation, and telemetry content.
- Provider and optional-extra compatibility matrix generated or tested against pinned SDK versions.
- Troubleshooting and upgrade guides.
- Honest limitations and a roadmap without promising dates.

## P1: release engineering

- Use protected environments and PyPI Trusted Publishing through GitHub Actions; do not store a long-lived PyPI token.
- Publish to TestPyPI first and install/test that exact artifact.
- Trigger production publication from a signed version tag or an approved release workflow.
- Generate release notes from curated changelog entries, not raw commit messages.
- Produce provenance attestations, hashes, SBOM, wheel, and source distribution.
- Consider Sigstore signing and verify artifacts in CI.
- Make releases immutable. Never replace an uploaded version.
- Test the CLI and package on clean macOS and Linux environments; add Windows only if it is supported and tested.
- Define a rollback response: yank a broken release, publish a fixed patch, and communicate impact.

## Recommended versioning

Keep `0.x` while public contracts, persisted schemas, and workflow semantics are changing. A reasonable progression is:

- `0.1.0a1`: private TestPyPI artifact and installation validation.
- `0.1.0b1`: public beta after security defaults, CI, license, package metadata, and quickstart are complete.
- `0.1.0`: first supported early-access release with a documented compatibility policy.
- `1.0.0`: only after public APIs, manifests, persisted state, migrations, and upgrade behavior are stable and the distributed production path is proven.

Do not use the existing hard-coded `0.1.0` as the first public stable release merely because it is already present in source.

## PyPI publication runbook

Run this only after the blockers above are complete:

1. Create an isolated build environment using a supported Python version.
2. Install the build frontend and metadata checker.
3. Run lint, type checks, all offline tests, service-backed integration tests, and package smoke tests.
4. Remove old `dist/` artifacts.
5. Build wheel and source distribution with `python -m build`.
6. Inspect archive contents and run `python -m twine check dist/*`.
7. Install each artifact into a clean environment and run imports, CLI help, server startup, migrations, and a mock agent.
8. Publish through Trusted Publishing to TestPyPI.
9. Install from TestPyPI in another clean environment and rerun the smoke test.
10. Create the signed release tag and approved GitHub release.
11. Publish the already-tested artifacts to production PyPI through Trusted Publishing.
12. Verify the PyPI page, metadata, hashes, installation command, import, CLI, and vulnerability scan.
13. Monitor issues and have maintainers available for the release window.

## Launch criteria

The repository is ready for a public beta when:

- License, ownership, trademark, security, and contributor governance are resolved.
- No secrets or proprietary data remain anywhere in Git history.
- CI passes on every supported Python version and required backend.
- Built artifacts pass clean-install tests and contain all runtime package data.
- The default deployment does not trust caller-supplied tenant identity on a public bind.
- One single-agent and one multi-agent path are genuinely runnable and tested.
- Quickstart, API reference, security guide, limitations, and upgrade policy are published.
- Maintainers have a triage, vulnerability, release, and rollback process.

## Adoption priorities after launch

1. Measure time-to-first-success, validation failures, example completion, and export success without collecting prompt content by default.
2. Publish a small number of excellent enterprise reference implementations rather than many unmaintained integrations.
3. Maintain framework/provider compatibility in CI and publish the results.
4. Make migration from existing LangGraph, OpenAI Agents, AutoGen, and CrewAI applications a first-class path.
5. Build community trust through predictable releases, transparent limitations, fast security response, and stable extension contracts.
