# Public repository settings

Several launch controls live in GitHub and PyPI rather than in version-controlled files. A repository owner must complete and verify this checklist before changing visibility or publishing a package.

## Repository creation and history

- Use the canonical repository name `AlgenAI/algen-agent-runtime`.
- Do not publish the current private Git history as-is. Start the public repository from an audited clean snapshot, or complete an owner-approved history rewrite and re-scan every ref.
- Verify author metadata intended for public disclosure.
- Set `main` as the default branch and configure the repository description, website, topics, and social preview.

## Branch and tag rulesets

Protect `main` with a ruleset that:

- requires pull requests and at least one approving review;
- requires CODEOWNERS review for owned paths;
- dismisses stale approvals after new commits;
- requires all review conversations to be resolved;
- requires the Quality, Security, and DCO checks;
- blocks force pushes and branch deletion;
- limits bypass permission to named maintainers and records every bypass.

Protect tags matching `v*` with a tag ruleset (`Release tags`) that blocks updates, deletion and
force pushes. Tag creation is deliberately not restricted: the Auto Release workflow creates the
tag with `GITHUB_TOKEN`, which a creation restriction would block. A stray `v*` tag cannot publish
a release: pushing a tag starts nothing (the Release workflow runs only through
`workflow_dispatch`), the workflow checks that the tag matches the package version, and its
publish job waits for a reviewer of the `pypi` environment. Release artifacts carry GitHub build
provenance and PEP 740 attestations (see [releasing](releasing.md)).

## Security features

- Enable the dependency graph, Dependabot alerts, and Dependabot security updates.
- Enable private vulnerability reporting and repository security advisories.
- Enable secret scanning and push protection, including generic-secret detection when available.
- Enable code scanning and make high-severity CodeQL findings merge-blocking.
- Grant the default workflow token read-only permissions, and require actions to be pinned to a
  full-length commit SHA (Settings → Actions → General). Every workflow already pins SHAs, and
  `tests/unit/test_workflow_supply_chain.py` fails on a tag-pinned action.
- Store no personal access tokens or other long-lived credentials in repository secrets; the
  workflows need only `GITHUB_TOKEN` and the `pypi` environment's OIDC token.
- Treat changes to `.github/workflows/release.yml`, `SECURITY.md`, `GOVERNANCE.md`, and CODEOWNERS as security-sensitive.

## PyPI publishing

- Reserve or verify ownership of the `algen-agent-runtime` PyPI project.
- Create a protected GitHub environment named `pypi` with required reviewers, prevent
  self-review, disallow administrator bypass where supported, and limit deployment branches to
  `main` (the Release workflow always runs from `main`, with the tag as an input).
- Configure PyPI Trusted Publishing for repository `AlgenAI/algen-agent-runtime`, workflow `release.yml`, and environment `pypi`.
- Do not store a long-lived PyPI API token in repository secrets.
- Publish only through the Release workflow for a tag that matches the package version.
- Verify PyPI provenance and clean installation after every release.

## Community features

- Enable Issues and apply the labels referenced by the issue forms (`bug`, `enhancement`, and `needs-triage`).
- Enable Discussions if maintainers want a separate channel for usage questions and design exploration.
- Confirm the private security-reporting link and public support links while signed out.
- Review the GitHub community profile after launch and resolve any missing-file warnings.

## Audit

Check these settings with the GitHub API before every release line and after any change of
maintainers:

```bash
gh api repos/AlgenAI/algen-agent-runtime/environments --jq '.environments[] | {name, protection_rules, deployment_branch_policy}'
gh api repos/AlgenAI/algen-agent-runtime/rulesets --jq '.[] | {name, target, enforcement}'
gh api repos/AlgenAI/algen-agent-runtime/actions/permissions
gh api repos/AlgenAI/algen-agent-runtime/actions/secrets --jq '.secrets[].name'
```

The 2026-11-15 security assessment found the `pypi` environment without required reviewers or a
branch policy, no `v*` tag ruleset, SHA pinning not required, Dependabot security updates off and
a `RELEASE_TOKEN` personal access token in the repository secrets; 0.2.0b4 removes the workflows'
use of that token.

On 2026-11-16 the owner resolved these findings:

- the `pypi` environment requires two reviewers, administrator bypass is off and deployment is
  limited to `main`;
- the `Release tags` ruleset protects `v*` tags (updates, deletion and force pushes blocked;
  creation open to the Auto Release workflow, as described above);
- actions must be pinned to a full-length commit SHA;
- Dependabot security updates are on;
- the `RELEASE_TOKEN` secret was deleted.

Record completion of this checklist in the launch or release issue so the public-readiness decision is auditable.
