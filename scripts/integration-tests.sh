#!/usr/bin/env bash
# Run the test suite against throwaway PostgreSQL and Redis containers, then remove them.
#
#   scripts/integration-tests.sh                 # pytest tests
#   scripts/integration-tests.sh tests/integration -k approvals
#
# Uses docker if it is installed, otherwise podman. Each run gets fresh containers (so a fresh
# database) on ports that do not clash with a local PostgreSQL or Redis; the containers are removed
# when the script exits, whether the tests pass, fail or are interrupted. Needs the
# `.[dev,postgres,redis]` extras in the Python environment.
set -euo pipefail

if command -v docker >/dev/null 2>&1; then
  engine=docker
elif command -v podman >/dev/null 2>&1; then
  engine=podman
else
  echo "integration-tests: neither docker nor podman is installed" >&2
  exit 2
fi

python="${PYTHON:-}"
if [[ -z "$python" ]]; then
  if [[ -x .venv/bin/python ]]; then python=.venv/bin/python; else python=python; fi
fi
"$python" -c "import asyncpg, redis" 2>/dev/null || {
  echo "integration-tests: install the extras first: $python -m pip install -e '.[dev,postgres,redis]'" >&2
  exit 2
}

pg_port="${ALGEN_TEST_POSTGRES_PORT:-55432}"
redis_port="${ALGEN_TEST_REDIS_PORT:-56379}"
suffix="$$"
pg_name="algen-rt-test-postgres-$suffix"
redis_name="algen-rt-test-redis-$suffix"
# A throwaway credential for a container that only lives for this run.
pg_password="algen_test_only"

cleanup() {
  "$engine" rm -f "$pg_name" "$redis_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

"$engine" run -d --rm --name "$pg_name" \
  -e POSTGRES_USER=algen -e POSTGRES_PASSWORD="$pg_password" -e POSTGRES_DB=algen_test \
  -p "$pg_port:5432" docker.io/library/postgres:17-alpine >/dev/null
"$engine" run -d --rm --name "$redis_name" \
  -p "$redis_port:6379" docker.io/library/redis:7-alpine >/dev/null

export POSTGRES_DSN="postgresql://algen:$pg_password@localhost:$pg_port/algen_test"
export REDIS_URL="redis://localhost:$redis_port/0"

# The postgres image restarts the server once after initdb, so wait for a real connection from
# the host rather than pg_isready inside the container.
for _ in $(seq 1 60); do
  if "$python" -c "import asyncio, asyncpg, os; asyncio.run(asyncpg.connect(os.environ['POSTGRES_DSN']))" \
    2>/dev/null; then
    break
  fi
  sleep 1
done
for _ in $(seq 1 30); do
  "$engine" exec "$redis_name" redis-cli ping 2>/dev/null | grep -q PONG && break
  sleep 1
done

echo "integration-tests: $engine, PostgreSQL on $pg_port, Redis on $redis_port"
if [[ $# -eq 0 ]]; then set -- tests; fi
"$python" -m pytest -q -p no:cacheprovider "$@"
