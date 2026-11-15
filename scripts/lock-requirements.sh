#!/usr/bin/env bash
# Regenerate the hash-locked requirement files in requirements/ on Linux (python:3.12-slim), the
# platform CI, the release workflow and the service image install them on.
#
#   requirements/build.txt     build, hatchling, twine: the release build installs only these
#   requirements/ci.txt        the package with the dev extra: lint, type check and tests
#   requirements/service.txt   the package with the service and postgres extras: the Dockerfile
#   requirements/security.txt  pip-audit and bandit for the Security workflow
#
# Run it after changing dependencies in pyproject.toml or requirements/*.in. Dependabot keeps the
# locks current between runs. Needs docker or podman.
set -euo pipefail

cd "$(dirname "$0")/.."
engine="$(command -v docker || command -v podman || true)"
if [ -z "$engine" ]; then
  echo "lock-requirements: docker or podman is required" >&2
  exit 1
fi

"$engine" run --rm -v "$PWD":/src:Z -w /src python:3.12-slim bash -euo pipefail -c '
python -m pip install -q --root-user-action=ignore "pip-tools>=7.4,<8"
export CUSTOM_COMPILE_COMMAND="scripts/lock-requirements.sh"
compile() { echo "== $*"; pip-compile -q --pip-args "--timeout 120 --retries 10" --generate-hashes --allow-unsafe --strip-extras --no-emit-index-url "$@"; }
compile -o requirements/build.txt requirements/build.in
compile -o requirements/security.txt requirements/security.in
compile --extra dev -o requirements/ci.txt pyproject.toml
compile --extra service --extra postgres -o requirements/service.txt pyproject.toml
'
echo "lock-requirements: requirements/*.txt updated"
