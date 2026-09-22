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
- The protected default branch is green on all required quality and security checks.
- The `pypi` GitHub environment has required reviewers.
- PyPI trusts `.github/workflows/release.yml` for the `AlgenAI/algen-agent-runtime` repository and `pypi` environment.
- The release maintainer has reviewed dependency, secret-scanning, CodeQL, and container findings.
- The owner-controlled settings in [repository-settings.md](repository-settings.md) are enabled and verified.

## Release steps

1. Merge a release pull request updating the version and changelog.
2. Create a GitHub release whose tag is exactly `v<package-version>`.
3. The release workflow verifies the tag, runs tests, builds the wheel and source distribution, generates `SHA256SUMS`, inspects contents, tests a clean installation outside the repository checkout, and uploads artifacts.
4. An authorized reviewer approves the protected `pypi` environment.
5. Trusted Publishing uploads the exact artifacts built by the workflow.
6. Verify the PyPI metadata, provenance, clean installation, CLI, and offline mock run:
   ```bash
   python -m venv /tmp/test-venv
   /tmp/test-venv/bin/pip install --upgrade pip
   /tmp/test-venv/bin/pip install --pre algen-agent-runtime
   /tmp/test-venv/bin/algen-agent-runtime --help
   /tmp/test-venv/bin/algen-agent-runtime new smoke_agent --template agent
   cd smoke_agent && /tmp/test-venv/bin/python app.py
   ```
7. Announce the release with known limitations and upgrade guidance.

## Artifact verification

Users and participants can verify the integrity of downloaded distribution artifacts before installation:

```bash
# Verify checksums
sha256sum -c SHA256SUMS
```

The SHA256 checksums are produced in the CI build step directly from the generated wheel and source archive.

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
