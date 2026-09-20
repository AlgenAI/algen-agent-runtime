# Governance

## Stewardship

Algen Agent Runtime and Algen Agent Studio are stewarded by AlgenAI. Traccia is
an optional telemetry integration and remains separately named. Inclusion of
that integration does not transfer ownership of either project's trademarks.

The authoritative maintainer list and current responsibilities are published in
[MAINTAINERS.md](MAINTAINERS.md) and mirrored by the protected CODEOWNERS
configuration.

## Decisions

Routine changes are decided through reviewed pull requests. Changes to public
APIs, persisted schemas, security boundaries, licensing, governance, or release
policy require a documented proposal and approval from all available maintainers.
When only one maintainer is active, these changes must pass protected checks and
receive a clearly recorded rationale before merge. When consensus cannot be
reached, the release maintainer records the decision and rationale in the pull
request or an architecture decision record.

## Roles

- Contributors submit issues, reviews, documentation, and code under the DCO.
- Maintainers triage work, review changes, enforce project boundaries, and
  nominate new maintainers based on sustained, trusted contributions.
- Release maintainers approve versions, artifacts, changelogs, publication,
  yanks, and security patches.

When two or more maintainers are active, no person may solely author and approve
a security-sensitive release change. During single-maintainer bootstrap,
protected CI and the manually approved release environment remain mandatory.

## Releases and succession

Releases use semantic versioning, immutable artifacts, protected environments,
and PyPI Trusted Publishing. A release requires a passing protected branch, an
approved changelog, and artifact verification. If a release maintainer becomes
inactive, the remaining maintainers nominate a successor and update CODEOWNERS,
protected environments, PyPI ownership, signing identities, and security access
before the next release.
