# Release process

Releases are built from signed or protected tags and published to PyPI through a dedicated GitHub Actions workflow using PyPI Trusted Publishing. Maintainers must not upload release artifacts from a workstation.

## Distribution channels

Algen Agent Runtime supports three reproducible distribution channels:

1. **PyPI (Primary channel):**
   Install the latest available release from PyPI:
   ```bash
   pip install algen-agent-runtime
   # Or for pre-releases (e.g. 0.1.0a1):
   pip install --pre algen-agent-runtime
   ```

2. **GitHub Releases:**
   Every release tag `v<version>` includes pre-built `.whl` and `.tar.gz` assets and a `SHA256SUMS` manifest.
   Download the wheel directly and install:
   ```bash
   pip install algen_agent_runtime-<version>-py3-none-any.whl
   ```

3. **Direct Git Tag:**
   Install directly from the pinned tag in source control:
   ```bash
   pip install git+https://github.com/AlgenAI/algen-agent-runtime.git@v<version>
   ```

## Prerequisites

- The target version is set in `src/algen_agent_runtime/__init__.py`.
- `CHANGELOG.md` contains the release date, changes, compatibility notes, and known limitations.
  A release that lets hosts delete code of their own (a capability they may already have built)
  says so under `### You can now remove`, after the upgrade notes; the format is described at the
  top of `CHANGELOG.md`.
  The Auto Release workflow takes the release notes from this file only; the `0.1` history is in
  `CHANGELOG-0.1.md`, so a `0.1` backport release puts its entry in `CHANGELOG.md`.
- The protected default branch is green on all required quality and security checks.
- The `pypi` GitHub environment has required reviewers.
- PyPI trusts `.github/workflows/release.yml` for the `AlgenAI/algen-agent-runtime` repository and `pypi` environment.
- The release maintainer has reviewed dependency, secret-scanning, CodeQL, and container findings.
- The owner-controlled settings in [repository-settings.md](repository-settings.md) are enabled and verified.

## Release steps

### Documentation gate

Before declaring a version ready, update the Runtime documentation page, `docs/docs.html`. It is
the site (`https://algenai.github.io/algen-agent-runtime/`) and its only source: Studio redirects
`/runtime/` there and its assistant reads it, but keeps no copy. Add the release with its final
configuration, defaults, compatibility notes, limitations, and examples, check that its
navigation works, and keep every link absolute (the Tools docs are
`https://algenai.github.io/algen-agent-tools/`). The `Docs` workflow publishes it to GitHub Pages
when it reaches `main`. Run `gitleaks detect --no-git --source docs` before committing: the
`secrets` check scans every commit on every branch, and it reads some ordinary prose as a key.
Reword any finding rather than quoting it. This documentation gate does not itself bump Studio's
Runtime dependency floor or publish a release.

The version and changelog release date should be finalized only after the feature and quality gates
are complete; an unreleased feature branch is not evidence of release readiness.

Run `python -m algen_agent_runtime_examples.context_evaluation` and the base test suite for deterministic context
regressions. Review [context evaluation limitations](context-evaluation.md) separately from any
deployment-specific model-quality evidence. Include the new diagnostic event and evaluation API
in the Studio documentation sync.

### Publish

1. Merge a release pull request updating the version and changelog.
2. The Auto Release workflow creates a missing `v<package-version>` tag and GitHub release on a
   push to `main`, then starts the Release workflow for that tag (`gh workflow run`, with the
   workflow's own `GITHUB_TOKEN`; no personal access token is involved). Merging the version bump
   is therefore a release action: review required quality and security results before merging. To
   publish an existing tag by hand, run the Release workflow from `main` with that tag; creating a
   GitHub release in the web UI publishes nothing.
3. The Release workflow, from `main`, checks out the tag and runs separate jobs:
   - `verify`: the tag format and version, lint, mypy and the unit suite, from the hash-locked
     `requirements/ci.txt`;
   - `build`: the sdist and wheel with only the hash-locked `requirements/build.txt` installed
     (`python -m build --no-isolation`), `twine check`, `SHA256SUMS`, `scripts/check_dist.py`;
   - `smoke`: a clean install outside the checkout (`scripts/smoke-install.sh`);
   - `sbom`: a CycloneDX SBOM and license report for the wheel plus the locked service
     dependencies (`scripts/sbom.py`, which fails on a license outside its allowed list).
   No job restores a cache, and every action is pinned to a commit SHA.
4. An authorized reviewer approves the protected `pypi` environment.
5. The `publish` job attests the exact files (GitHub build provenance) and uploads them with
   Trusted Publishing, which adds PEP 740 attestations on PyPI. `release-assets` then attaches
   `SHA256SUMS`, `sbom.cdx.json` and `licenses.md` to the GitHub release.
6. Verify the PyPI metadata, provenance, clean installation, CLI, and offline mock run:
   ```bash
   python -m venv /tmp/test-venv
   /tmp/test-venv/bin/pip install --upgrade pip
   /tmp/test-venv/bin/pip install --pre 'algen-agent-runtime[service]'
   /tmp/test-venv/bin/algen-agent-runtime --help
   /tmp/test-venv/bin/algen-agent-runtime new smoke_agent --template agent
   cd smoke_agent && /tmp/test-venv/bin/python app.py
   ```
7. Announce the release with known limitations and upgrade guidance.

## Artifact verification

Users and participants can verify the integrity of downloaded distribution artifacts before installation:

```bash
# Checksums (SHA256SUMS is attached to the GitHub release)
sha256sum -c SHA256SUMS

# GitHub build provenance: the file was built by this repository's release workflow
gh attestation verify algen_agent_runtime-<version>-py3-none-any.whl --repo AlgenAI/algen-agent-runtime
```

PyPI shows the PEP 740 provenance of each file under "Verified details", and
`https://pypi.org/integrity/algen-agent-runtime/<version>/<file>/provenance` returns it. The SBOM
(`sbom.cdx.json`) and license report (`licenses.md`) on the GitHub release list what a deployment
of the release installs with the `service` and `postgres` extras.

### Dependency locks

`requirements/*.txt` are hash-locked (`pip-compile --generate-hashes`) on Linux by
`scripts/lock-requirements.sh`, from `pyproject.toml` and `requirements/*.in`: `build.txt` (the
release build), `ci.txt` (lint, type check, tests), `service.txt` (the container image) and
`security.txt` (the scanners). Regenerate them after changing dependencies; Dependabot proposes
updates between releases. The package's own dependency ranges in `pyproject.toml` stay open, so
applications resolve them with their other dependencies.

## Failure, rollback, and yanking procedure

Do not reuse or overwrite an existing release tag or published version number; once a version tag is published, it remains immutable. If a release contains a defect or security issue:

1. **Halt Announcements & Triage:** Immediately stop marketing/hackathon announcements and assess the blast radius.
2. **Yank on PyPI:**
   - Mark the affected release files as yanked via PyPI Web UI (**Manage Project -> Releases -> Options -> Yank Release**) or via `twine`:
     ```bash
     twine yank --reason "Critical regression: see advisory GHSA-xxx" algen-agent-runtime <version>
     ```
   - Yanking prevents the package resolver from selecting this version for unpinned installs while preserving compatibility for existing pinned lockfiles.
3. **Annotate GitHub Release:**
   - Edit the GitHub Release notes to add a prominent warning banner stating that the version is yanked and why.
   - If severe, convert the GitHub Release to a Draft or mark as Pre-release with deprecation notice.
4. **Publish Security Advisory:**
   - If the issue is a security vulnerability, publish a GitHub Security Advisory (GHSA) with affected versions and remediation.
5. **Issue Patch Release:**
   - Increment the version in `src/algen_agent_runtime/__init__.py` (e.g. `0.1.0a2`).
   - Document the fix and the yanked version context in `CHANGELOG.md`.
   - Tag and publish the new patch release following the standard release process.
