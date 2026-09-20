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

Protect tags matching `v*` so only release maintainers can create or delete them. Require signed tags or organization-approved provenance when available.

## Security features

- Enable the dependency graph, Dependabot alerts, and Dependabot security updates.
- Enable private vulnerability reporting and repository security advisories.
- Enable secret scanning and push protection, including generic-secret detection when available.
- Enable code scanning and make high-severity CodeQL findings merge-blocking.
- Review Actions allowed-action policy and grant the default workflow token read-only permissions.
- Treat changes to `.github/workflows/release.yml`, `SECURITY.md`, `GOVERNANCE.md`, and CODEOWNERS as security-sensitive.

## PyPI publishing

- Reserve or verify ownership of the `algen-agent-runtime` PyPI project.
- Create a protected GitHub environment named `pypi` with required reviewers and disallow administrator bypass where supported.
- Configure PyPI Trusted Publishing for repository `AlgenAI/algen-agent-runtime`, workflow `release.yml`, and environment `pypi`.
- Do not store a long-lived PyPI API token in repository secrets.
- Publish only through a GitHub release whose tag matches the package version.
- Verify PyPI provenance and clean installation after every release.

## Community features

- Enable Issues and apply the labels referenced by the issue forms (`bug`, `enhancement`, and `needs-triage`).
- Enable Discussions if maintainers want a separate channel for usage questions and design exploration.
- Confirm the private security-reporting link and public support links while signed out.
- Review the GitHub community profile after launch and resolve any missing-file warnings.

Record completion of this checklist in the launch or release issue so the public-readiness decision is auditable.
