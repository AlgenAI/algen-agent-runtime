#!/usr/bin/env bash
# Install a built wheel in a fresh virtual environment outside the checkout and use it the way a
# user would: the bare library, `serve` without and with the server extra, the API app, migrations,
# starter tools, the release examples, and a scaffolded project's tests and app.
#
# CI's clean-install job and the release workflow run this, and so does scripts/ci-local.sh.
#
# Usage: scripts/smoke-install.sh DIST_DIR [PYTHON]
set -euo pipefail

dist="$(cd "${1:?usage: scripts/smoke-install.sh DIST_DIR [PYTHON]}" && pwd)"
python="${2:-python3}"
wheel="$(ls "$dist"/*.whl)"
work="$(mktemp -d "${TMPDIR:-/tmp}/algen-smoke.XXXXXX")"
trap 'rm -rf "$work"' EXIT
venv="$work/venv"

"$python" -m venv "$venv"
"$venv/bin/pip" install -q --upgrade pip
"$venv/bin/pip" install -q "$wheel"
cd "$work"

"$venv/bin/python" -c "import algen_agent_runtime; print('installed', algen_agent_runtime.__version__)"
test -x "$venv/bin/algen-agent-runtime"
"$venv/bin/algen-agent-runtime" --help >/dev/null

# The base package is the library: serve names the extra instead of a traceback.
if "$venv/bin/algen-agent-runtime" serve 2>serve.err; then
  echo "smoke-install: serve started without the server extra" >&2
  exit 1
fi
grep -q "algen-agent-runtime\[server\]" serve.err

"$venv/bin/pip" install -q "${wheel}[service]"
"$venv/bin/algen-agent-runtime" serve --help >/dev/null
"$venv/bin/python" -c "from algen_agent_runtime.api.app import create_app; assert create_app().title == 'Algen Agent Runtime'"
"$venv/bin/python" -c "from importlib.resources import files; assert files('algen_agent_runtime').joinpath('persistence/migrations/001_initial.sql').is_file()"
"$venv/bin/python" -c "from algen_agent_runtime.tools.starter import get_starter_tools; assert len(get_starter_tools()) >= 5"

for example in context_evaluation context_compaction context_offloading working_state \
  progressive_skills learned_memory quickstart_conversation.app quickstart_attachments.app \
  pattern_guarded_tools.app pattern_side_effect_reconciliation.app pattern_secured_api.app; do
  "$venv/bin/python" -m "algen_agent_runtime_examples.${example}" >/dev/null
done

"$venv/bin/algen-agent-runtime" new smoke_agent --template agent >/dev/null
cd smoke_agent
"$venv/bin/pip" install -q pytest pytest-asyncio
"$venv/bin/pytest" -q -p no:cacheprovider test_agent.py
"$venv/bin/python" app.py >/dev/null
echo "smoke-install: OK ($("$python" -V))"
