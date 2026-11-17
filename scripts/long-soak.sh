#!/usr/bin/env bash
# The long soak: the service image under hours of mixed HTTP load, with restarts, on PostgreSQL 17
# and Redis 7 containers. See tests/perf/long_soak/README.md.
#
# Usage: scripts/long-soak.sh OUT_DIR [driver arguments...]
#   SOAK_IMAGE        image to run (default: build algen-agent-runtime:soak from the Dockerfile)
#   SOAK_SECONDS      seconds of load (default 10800, three hours)
#   SOAK_PG_PORT      host port for PostgreSQL (default 55515)
#   SOAK_REDIS_PORT   host port for Redis (default 56515)
#   SOAK_PORT         host port for the Runtime (default 18505)
#   SOAK_KEEP=1       leave the containers running afterwards (remove them with the names below)
#   PYTHON            Python with httpx, asyncpg, redis and PyJWT[crypto] (default python3)
#
# Containers are named algen-rt-soak-{postgres,redis,runtime} and are removed at the end unless
# SOAK_KEEP=1; a fresh database every run.
set -euo pipefail

cd "$(dirname "$0")/.."
root="$PWD"
out="${1:?usage: scripts/long-soak.sh OUT_DIR [driver arguments...]}"
shift
engine="$(command -v docker || command -v podman || true)"
if [ -z "$engine" ]; then
  echo "long-soak: docker or podman is required" >&2
  exit 1
fi
python="${PYTHON:-python3}"
seconds="${SOAK_SECONDS:-10800}"
pg_port="${SOAK_PG_PORT:-55515}"
redis_port="${SOAK_REDIS_PORT:-56515}"
port="${SOAK_PORT:-18505}"
network="algen-rt-soak-net"
pg="algen-rt-soak-postgres"
redis="algen-rt-soak-redis"
runtime="algen-rt-soak-runtime"

mkdir -p "$out"
out="$(cd "$out" && pwd)"
mkdir -p "$out/keys" "$out/effects" "$out/stats"
chmod 0777 "$out/effects" "$out/stats"

cleanup() {
  if [ "${SOAK_KEEP:-0}" != "1" ]; then
    "$engine" logs "$runtime" >"$out/runtime.log" 2>&1 || true
    "$engine" rm -f -v "$runtime" "$redis" "$pg" >/dev/null 2>&1 || true
    "$engine" network rm "$network" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

image="${SOAK_IMAGE:-}"
if [ -z "$image" ]; then
  image="algen-agent-runtime:soak"
  "$engine" build -q -t "$image" . >/dev/null
fi

# A throwaway RS256 key pair: the driver signs tokens, the Runtime verifies them.
openssl genrsa -out "$out/keys/private.pem" 2048 2>/dev/null
openssl rsa -in "$out/keys/private.pem" -pubout -out "$out/keys/public.pem" 2>/dev/null

"$engine" rm -f -v "$runtime" "$redis" "$pg" >/dev/null 2>&1 || true
"$engine" network rm "$network" >/dev/null 2>&1 || true
"$engine" network create "$network" >/dev/null
password="soak-$(openssl rand -hex 8)"
"$engine" run -d --name "$pg" --network "$network" -p "127.0.0.1:${pg_port}:5432" \
  -e POSTGRES_USER=algen -e POSTGRES_PASSWORD="$password" -e POSTGRES_DB=algen_soak \
  postgres:17-alpine -c max_connections=100 -c max_wal_size=256MB -c min_wal_size=64MB >/dev/null
"$engine" run -d --name "$redis" --network "$network" -p "127.0.0.1:${redis_port}:6379" \
  redis:7-alpine >/dev/null
for _ in $(seq 1 60); do
  "$engine" exec "$pg" pg_isready -U algen -d algen_soak >/dev/null 2>&1 && break
  sleep 1
done
sleep 2

"$engine" run -d --name "$runtime" --network "$network" -p "127.0.0.1:${port}:8000" \
  -v "$root/tests/perf/long_soak:/soak/plugin:ro,Z" \
  -v "$out/effects:/soak/effects:Z" \
  -v "$out/stats:/soak/stats:Z" \
  -e PYTHONPATH=/soak/plugin \
  -e ALGEN_AGENT_RUNTIME_CONFIG=/soak/plugin/runtime.yaml \
  -e ALGEN_AGENT_RUNTIME_POSTGRES_DSN="postgresql://algen:${password}@${pg}:5432/algen_soak" \
  -e ALGEN_AGENT_RUNTIME_REDIS_URL="redis://${redis}:6379/0" \
  -e ALGEN_AGENT_RUNTIME_JWT_PUBLIC_KEY="$(cat "$out/keys/public.pem")" \
  -e SOAK_EFFECTS_DIR=/soak/effects \
  -e SOAK_STATS_DIR=/soak/stats \
  "$image" >/dev/null

ready=""
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:${port}/health/ready" >/dev/null 2>&1; then
    ready=yes
    break
  fi
  sleep 1
done
if [ -z "$ready" ]; then
  echo "long-soak: the Runtime never became ready" >&2
  "$engine" logs "$runtime" >&2 || true
  exit 1
fi
{
  echo "commit=$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "image=$image $("$engine" image inspect -f '{{.Id}}' "$image" 2>/dev/null | cut -c1-19)"
  echo "engine=$("$engine" --version)"
  echo "postgres=$("$engine" exec "$pg" postgres --version)"
  echo "redis=$("$engine" exec "$redis" redis-server --version | cut -d' ' -f3)"
  echo "host=$(uname -mrs)"
  echo "started=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$out/environment.txt"

"$python" -I "$root/tests/perf/long_soak/driver.py" \
  --base-url "http://127.0.0.1:${port}" \
  --private-key "$out/keys/private.pem" \
  --postgres-dsn "postgresql://algen:${password}@127.0.0.1:${pg_port}/algen_soak" \
  --redis-url "redis://127.0.0.1:${redis_port}/0" \
  --engine "$engine" \
  --runtime-container "$runtime" \
  --postgres-container "$pg" \
  --effects-dir "$out/effects" \
  --stats-dir "$out/stats" \
  --out "$out" \
  --duration "$seconds" \
  "$@"
