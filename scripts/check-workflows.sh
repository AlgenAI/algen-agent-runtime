#!/usr/bin/env bash
# Audit .github/ (workflows and dependabot.yml) with zizmor, from the hash-locked
# requirements/security.txt. It fails on an action pinned by a tag instead of a commit SHA, a
# workflow input or event field interpolated into a run: script, persist-credentials left on,
# excessive permissions, a dangerous trigger and a dependency update without a cooldown: the
# supply-chain findings of the 0.2.0b3 security assessment (SC-1 to SC-10).
#
# With GH_TOKEN set (as in the Security workflow) zizmor also runs its online audits: impostor
# commits, known-vulnerable actions and refs that resolve to more than one commit. Without it,
# the offline audits run.
#
# A finding that is safe in context is suppressed where it occurs, with the reason:
#   on:
#     pull_request_target: # zizmor: ignore[dangerous-triggers] read-only, never runs PR code
#
# Usage: scripts/check-workflows.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python="${PYTHON:-python3}"
work="$(mktemp -d "${TMPDIR:-/tmp}/algen-check-workflows.XXXXXX")"
trap 'rm -rf "$work"' EXIT

"$python" -m venv "$work/env"
"$work/env/bin/pip" install -q --isolated --require-hashes --no-deps -r requirements/security.txt
online=(--offline)
[[ -n "${GH_TOKEN:-}" ]] && online=()
"$work/env/bin/zizmor" "${online[@]}" --format plain .github/
