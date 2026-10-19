# a7 release preparation

Prepared on 2026-10-19. Version `0.1.0a7` is a release candidate on
`feat/context-management`; it has not been tagged or published by this preparation.

## Verified locally

- Runtime formatting, lint, and typing pass (136 typed source files).
- Runtime suite: 770 passed, 21 skipped; coverage 83.95% against the 75% gate.
- Clean wheel installation with only the `dev` extra: 759 passed, 32 skipped. Optional
  database, MCP, object-storage, model, and telemetry integrations were absent.
- The additional release-workflow regression test passes in both environments. It proves
  that shell-like release-note text stays literal and the tag targets the triggering commit.
- Wheel and source distribution build successfully and pass `twine check`. Artifact inspection
  verifies context modules, migration 011, package metadata, and the Runtime/Studio boundary.
- All six packaged context examples run from outside the checkout. Generated agent scaffolding
  passes its test and offline execution using the installed wheel.
- Studio source and served Runtime documentation copies match. Their configuration example
  validates against Runtime contracts; internal anchors, JavaScript syntax, rendering, and a6/a7
  version navigation were checked.
- Studio checks: formatting, lint, typing, contract parity, 772 backend tests passed (11 skipped,
  PostgreSQL integration enabled), 98 frontend tests passed, and production build passed.

Skipped optional/live tests are not evidence that those integrations passed. Deterministic context
evaluations are not evidence of production-model summary accuracy.

## Required before publication

1. Run the feature branch through pull-request Quality, Security, DCO, and container checks.
   No feature-branch check runs existed at preparation time. Green checks on `main` cover its
   existing commit, not these changes. Review secret-scanning and dependency findings.
2. Configure required reviewers on GitHub's `pypi` environment. The environment existed but
   returned an empty `protection_rules` list when checked. The release policy requires approval.
3. Verify the PyPI Trusted Publisher binding for this repository, `release.yml`, and `pypi`.
   `RELEASE_TOKEN` is configured in GitHub; its value and validity were not inspected.
4. Review the release version/date and merge only when ready to release. Auto Release creates
   a missing version tag on pushes to `main`; merging this bump can trigger publication.
5. Approve the protected publication job, verify the published package and provenance, and
   update documentation that currently labels a7 as pending publication.

Local artifacts are verification outputs. Publish only the artifacts rebuilt by the protected
GitHub release workflow, as described in [the release process](releasing.md).
