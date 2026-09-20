# Release process

Releases are built from signed or protected tags and published to PyPI through a dedicated GitHub Actions workflow using PyPI Trusted Publishing. Maintainers must not upload release artifacts from a workstation.

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
3. The release workflow verifies the tag, runs tests, builds the wheel and source distribution, validates metadata and contents, and uploads the artifacts for review.
4. An authorized reviewer approves the protected `pypi` environment.
5. Trusted Publishing uploads the exact artifacts built by the workflow.
6. Verify the PyPI metadata, provenance, clean installation, CLI, and offline mock run.
7. Announce the release with known limitations and upgrade guidance.

## Failure and yanking

Do not reuse or overwrite a published version. If a release is unsafe or unusable, stop announcements, publish a security advisory when appropriate, yank the affected files on PyPI with a reason, and issue a new patch release. Record the decision in the changelog and GitHub release notes.
