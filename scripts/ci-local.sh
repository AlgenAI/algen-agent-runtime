#!/usr/bin/env bash
# Build and test the way CI does, before pushing.
#
# 1. Build the sdist and then the wheel from the sdist (`python -m build`), as CI and the release
#    workflow do. A wheel built straight from the checkout can contain files the sdist leaves out.
# 2. `twine check` and scripts/check_dist.py on the result.
# 3. scripts/smoke-install.sh with each Python CI uses (3.12 and 3.13, when installed).
# 4. The base CI job on Linux: lint, mypy and the unit suite with only `.[dev]` installed, in a
#    python:3.12-slim container (docker or podman). macOS passing proves nothing about Linux.
#
# Usage: scripts/ci-local.sh [--no-linux]
# Integration tests are separate: scripts/integration-tests.sh (AGENTS.md section 3).
set -euo pipefail
cd "$(dirname "$0")/.."

linux=1
[[ "${1:-}" == "--no-linux" ]] && linux=0

python="${PYTHON:-}"
if [[ -z "$python" ]]; then
  if [[ -x .venv/bin/python ]]; then python=.venv/bin/python; else python=python3; fi
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/algen-ci-local.XXXXXX")"
trap 'rm -rf "$work"' EXIT

echo "== build (sdist, then the wheel from the sdist)"
"$python" -m pip install -q --isolated build twine
"$python" -m build -q --outdir "$work/dist" .
"$python" -m twine check "$work/dist"/*
"$python" scripts/check_dist.py "$work/dist"

for version in 3.12 3.13; do
  if command -v "python$version" >/dev/null; then
    echo "== clean install, Python $version"
    scripts/smoke-install.sh "$work/dist" "python$version"
  else
    echo "== clean install, Python $version: not installed, skipped"
  fi
done

if (( linux )); then
  engine="$(command -v docker || command -v podman || true)"
  if [[ -z "$engine" ]]; then
    echo "ci-local: install docker or podman, or pass --no-linux" >&2
    exit 1
  fi
  echo "== base CI job on Linux ($(basename "$engine"), python:3.12-slim, .[dev] only)"
  "$engine" run --rm -v "$PWD:/src:ro" python:3.12-slim bash -euo pipefail -c '
    cp -r /src /w && cd /w && rm -rf .venv .claude
    pip install -q --root-user-action=ignore --retries 5 --timeout 60 -e ".[dev]"
    ruff format --check src tests algen_agent_runtime_examples
    ruff check src tests algen_agent_runtime_examples
    mypy src/algen_agent_runtime
    python -m pytest -q -p no:cacheprovider tests --ignore=tests/integration'
fi
echo "ci-local: OK"
