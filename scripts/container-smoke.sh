#!/usr/bin/env bash
# Start the service image and use it: readiness, one run on the mock provider, then a graceful
# drain on SIGTERM. CI's container job and scripts/ci-local.sh run this after building the image.
#
# Usage: scripts/container-smoke.sh [IMAGE]   (default: builds algen-agent-runtime:smoke)
set -euo pipefail

cd "$(dirname "$0")/.."
engine="$(command -v docker || command -v podman || true)"
if [ -z "$engine" ]; then
  echo "container-smoke: docker or podman is required" >&2
  exit 1
fi
image="${1:-}"
if [ -z "$image" ]; then
  image="algen-agent-runtime:smoke"
  "$engine" build -q -t "$image" . >/dev/null
fi

name="algen-rt-smoke-$$"
work="$(mktemp -d "${TMPDIR:-/tmp}/algen-container-smoke.XXXXXX")"
cleanup() {
  "$engine" rm -f "$name" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

cat >"$work/runtime.yaml" <<'YAML'
providers:
  mock: {type: mock, default_model: deterministic}
agents:
  - name: smoke
    version: 1.0.0
    description: Container smoke test agent
    system_instructions: Answer briefly.
    default_model: {name: deterministic, provider: mock, model: deterministic}
    planning_strategy: direct
    max_steps: 2
YAML
chmod 0644 "$work/runtime.yaml"

# Development headers outside loopback need the explicit insecure flag; the container is reachable
# only from this host for the length of the test.
"$engine" run -d --name "$name" -p 127.0.0.1::8000 \
  -v "$work/runtime.yaml:/config/runtime.yaml:ro,Z" \
  -e ALGEN_AGENT_RUNTIME_CONFIG=/config/runtime.yaml \
  -e ALGEN_AGENT_RUNTIME__API__HOST=0.0.0.0 \
  -e ALGEN_AGENT_RUNTIME__API__ALLOW_INSECURE_DEVELOPMENT_AUTH=true \
  "$image" >/dev/null
port="$("$engine" port "$name" 8000/tcp | head -1 | sed 's/.*://')"
base="http://127.0.0.1:${port}"

ready=""
for _ in $(seq 1 60); do
  if curl -fsS "$base/health/ready" >/dev/null 2>&1; then
    ready=yes
    break
  fi
  sleep 1
done
if [ -z "$ready" ]; then
  echo "container-smoke: /health/ready never returned 200" >&2
  "$engine" logs "$name" >&2 || true
  exit 1
fi

headers=(-H "content-type: application/json" -H "x-tenant-id: smoke" -H "x-user-id: smoke-user"
  -H "x-scopes: runs:read runs:write")
run_id="$(curl -fsS "${headers[@]}" -d '{"agent":"smoke","input":"hello"}' "$base/v1/runs" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')"
status=""
for _ in $(seq 1 30); do
  status="$(curl -fsS "${headers[@]}" "$base/v1/runs/$run_id" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  [ "$status" = "completed" ] && break
  sleep 1
done
if [ "$status" != "completed" ]; then
  echo "container-smoke: run $run_id ended as '$status', expected completed" >&2
  "$engine" logs "$name" >&2 || true
  exit 1
fi

# SIGTERM drains (readiness 503, maintenance and workers stopped) and exits 0 within the grace.
"$engine" stop -t 30 "$name" >/dev/null
code="$("$engine" inspect -f '{{.State.ExitCode}}' "$name")"
if [ "$code" != "0" ]; then
  echo "container-smoke: the service exited with $code after SIGTERM" >&2
  "$engine" logs "$name" >&2 || true
  exit 1
fi
echo "container-smoke: ready, run $run_id completed, drained cleanly ($image)"
