# Release process

Releases are built from `v<version>` tags (protected against updates and deletion by the `Release tags` ruleset, see [repository settings](repository-settings.md#branch-and-tag-rulesets)) and published to PyPI through a dedicated GitHub Actions workflow using PyPI Trusted Publishing. Maintainers must not upload release artifacts from a workstation.

## Distribution channels

Algen Agent Runtime supports three reproducible distribution channels:

1. **PyPI (Primary channel):**
   Install the latest available release from PyPI:
   ```bash
   pip install algen-agent-runtime
   # Or for pre-releases (e.g. 0.2.0b4):
   pip install --pre algen-agent-runtime
   ```

2. **GitHub Releases:**
   Every release tag `v<version>` has a GitHub release with the release notes, a `SHA256SUMS`
   manifest of the wheel and sdist published to PyPI, a CycloneDX SBOM (`sbom.cdx.json`) and a
   license report (`licenses.md`). The wheel and sdist themselves are on PyPI; download them and
   check them against the manifest (see [Artifact verification](#artifact-verification)):
   ```bash
   pip download --no-deps --pre algen-agent-runtime==<version> -d dist/
   pip install dist/algen_agent_runtime-<version>-py3-none-any.whl
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
- The first release of a new minor line (`0.3.0`, `0.4.0`, ...) and any release that adds an HTTP
  route, an authentication mode, a way to run tools or a release-workflow change has had a
  security review: the rules in AGENTS.md, "Security invariants", checked against the change,
  and for a new minor line a full assessment of the API, governance, storage, plugins and supply
  chain like the one before `0.2.0b4`. Findings are fixed or tracked in a private security
  advisory before the release, never in a public issue.
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
deployment-specific model-quality evidence. Record what Studio's own Runtime documentation must
change for the release (new events, settings, scopes) in [Studio compatibility](studio-compatibility.md).

### Live provider contracts

Before every release, a maintainer runs the live provider contract suite (`tests/live`, marker
`live`) against the real vendor APIs with their own keys. It checks every capability that
[provider compatibility](provider-compatibility.md#live-provider-contracts) claims for each
built-in provider class, through the Runtime's own classes, `ModelRouter`, `build_container` and
`Runtime.run`. CI never runs it: no repository secret holds a vendor key, and no workflow may
read one. Without `ALGEN_LIVE=1` (or `ALGEN_LIVE_FAKE=1`) every test in it is collected and
skipped, so the base suite is unaffected.

```bash
# Keys come from your shell, never from a file in the checkout. Set only the ones you have:
# each provider without its variables is skipped and says which ones it needs.
export OPENAI_API_KEY=... ANTHROPIC_API_KEY=... GEMINI_API_KEY=... XAI_API_KEY=...
export GROQ_API_KEY=... MISTRAL_API_KEY=... DEEPSEEK_API_KEY=... HF_TOKEN=...
export AZURE_OPENAI_API_KEY=... AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_DEPLOYMENT=<chat deployment>        # optional: AZURE_OPENAI_API_VERSION,
                                                        # AZURE_OPENAI_EMBED_DEPLOYMENT
ALGEN_LIVE=1 ALGEN_LIVE_REPORT=live-report.md \
  python -m pytest -p no:cacheprovider tests/live -m live -rsx
```

- **Providers and models.** `ALGEN_LIVE_PROVIDERS=openai,anthropic` runs only some providers.
  Each uses a cheap default model, replaced with `ALGEN_LIVE_<PROVIDER>_MODEL` (and
  `ALGEN_LIVE_<PROVIDER>_EMBED_MODEL` for embeddings): `openai` `gpt-4o-mini`, `anthropic`
  `claude-haiku-4-5`, `gemini` `gemini-2.5-flash-lite`, `xai` `grok-4-fast-non-reasoning`, `groq`
  and `huggingface` `openai/gpt-oss-20b`, `mistral` `mistral-small-latest`, `deepseek`
  `deepseek-chat`, `ollama` `llama3.2`. Ollama runs when `ALGEN_LIVE_OLLAMA_BASE_URL` (default
  `http://127.0.0.1:11434/v1`) answers; pull the model first.
- **Cost.** Every request is a short prompt with `max_output_tokens` 512
  (`ALGEN_LIVE_MAX_OUTPUT_TOKENS`). A full run is about 30 small calls per provider, two of them
  with a 32x32 image or a one-page PDF; with the default models it costs well under one US dollar
  in total. The wrong-key checks send a key no vendor issues.
- **Keys.** The provider classes read keys through `env://` references; the suite never prints,
  logs or stores them, and the report holds outcomes only. A provider error that quoted the key
  it was sent fails the wrong-key check.
- **Reading failures.** A capability test runs only when the provider class claims the
  capability (`capabilities(model)`), so a failure means the Runtime claims something the live
  API did not do with that model: fix the class, narrow its default capabilities, or, when only
  some models of the vendor support it, pick a model that does with `ALGEN_LIVE_<PROVIDER>_MODEL`
  and say so in [provider compatibility](provider-compatibility.md). A failure of a wrong-key or
  unknown-model check means an HTTP error is mapped to the wrong `ErrorKind`, which changes
  whether the router retries and falls back. `xfail` marks a provider bug already known (listed
  in `KNOWN_BUGS` in the test module); an `XPASS` means it was fixed: remove the entry. A
  model's wording can occasionally miss an expected word: rerun that test once before treating it
  as a failure.
- **Record the result.** Paste the table `ALGEN_LIVE_REPORT` wrote (date, mode, one row per
  provider) into the results table of [provider compatibility](provider-compatibility.md#live-provider-contracts)
  in the release pull request, and say there which providers you could not run.

`ALGEN_LIVE_FAKE=1` runs the same suite offline against a local fake of each vendor's API
(`tests/live/live_fake_api.py`), with a placeholder key and no network beyond 127.0.0.1. It
proves the suite's own logic and the request shapes, not the vendors: use it after changing the
suite or a provider class, never instead of the live run.

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
# Checksums (SHA256SUMS is attached to the GitHub release; run in the directory holding the files)
sha256sum -c SHA256SUMS

# GitHub build provenance: the file was built by this repository's release workflow
gh attestation verify algen_agent_runtime-<version>-py3-none-any.whl --repo AlgenAI/algen-agent-runtime
```

PyPI shows the PEP 740 provenance of each file under "Verified details", and
`https://pypi.org/integrity/algen-agent-runtime/<version>/<file>/provenance` returns it. The SBOM
(`sbom.cdx.json`) and license report (`licenses.md`) on the GitHub release list what a deployment
of the release installs with the `service`, `postgres`, `auth` and `redis` extras.

### Dependency locks

`requirements/*.txt` are hash-locked (`pip-compile --generate-hashes`) on Linux by
`scripts/lock-requirements.sh`, from `pyproject.toml` and `requirements/*.in`: `build.txt` (the
release build), `ci.txt` (lint, type check, tests), `service.txt` (the container image) and
`security.txt` (the scanners). Regenerate them after changing dependencies; Dependabot proposes
updates between releases. The package's own dependency ranges in `pyproject.toml` stay open, so
applications resolve them with their other dependencies.

## Failure, rollback, and yanking procedure

Do not reuse or overwrite an existing release tag or published version number; once a version tag is published, it remains immutable. If a release contains a defect or security issue:

1. **Halt Announcements & Triage:** Immediately stop announcements and assess the blast radius.
2. **Yank on PyPI:**
   - Mark the affected release as yanked in the PyPI web UI (**Manage Project -> Releases -> Options -> Yank**), with a reason that names the advisory or issue. `twine` has no yank command.
   - Yanking prevents the package resolver from selecting this version for unpinned installs while preserving compatibility for existing pinned lockfiles.
3. **Annotate GitHub Release:**
   - Edit the GitHub Release notes to add a prominent warning banner stating that the version is yanked and why.
   - If severe, convert the GitHub Release to a Draft or mark as Pre-release with deprecation notice.
4. **Publish Security Advisory:**
   - If the issue is a security vulnerability, publish a GitHub Security Advisory (GHSA) with affected versions and remediation.
5. **Issue Patch Release:**
   - Increment the version in `src/algen_agent_runtime/__init__.py` (e.g. `0.2.0b4` to `0.2.0b5`).
   - Document the fix and the yanked version context in `CHANGELOG.md`.
   - Tag and publish the new patch release following the standard release process.
