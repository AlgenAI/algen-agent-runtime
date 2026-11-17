# Long soak (hours)

The service image from the repository's `Dockerfile`, under hours of mixed HTTP load, on
PostgreSQL 17 and Redis 7 containers, with restarts. Results from a run are in
[operations](../../../docs/operations.md#long-soak-hours).

```bash
python -m pip install --isolated -e '.[dev,postgres,redis,auth]'   # the driver's dependencies
SOAK_SECONDS=21600 PYTHON="$(command -v python)" scripts/long-soak.sh /tmp/soak-6h
python tests/perf/long_soak/report.py /tmp/soak-6h               # the tables, as Markdown
```

`scripts/long-soak.sh OUT_DIR [driver arguments]` builds the image (or uses `SOAK_IMAGE`), starts
`algen-rt-soak-postgres`, `algen-rt-soak-redis` and `algen-rt-soak-runtime` on a private network
(host ports `SOAK_PG_PORT` 55515, `SOAK_REDIS_PORT` 56515 and `SOAK_PORT` 18505), runs the driver
for `SOAK_SECONDS` (default three hours) and removes the containers, whatever the outcome. It
exits non-zero when an invariant did not hold.

## What runs

- **The Runtime**: the image, unchanged, with `runtime.yaml` (every store on PostgreSQL, session
  memory on Redis, a pool of 20, JWT RS256 auth, the admission limits recommended in
  `docs/operations.md`, the maintenance sweep every 30 s with one-day windows for terminal runs,
  audit events, conversations and workflow runs)
  and `soak_plugin.py` mounted read-only on `PYTHONPATH`. The plugin registers two scripted
  model providers (`soak-fast`, about 150 ms a call; `soak-slow`, about 3 s), a read tool
  (`soak.lookup`) and a write tool (`soak.record`) that appends one line per call to
  `OUT_DIR/effects/`, and a probe that writes the asyncio task count to `OUT_DIR/stats/`.
- **The load** (`driver.py`), per tenant `t-alpha`, `t-beta`, `t-gamma`: `--workers` (6) clients
  doing a weighted mix of runs followed over SSE (35 %), runs that pause for an approval which
  is approved, rejected or left to expire (20 %), workflows with an approval node and a tool step
  (10 %), slow runs cancelled mid-way (8 %), slow runs followed to the end (7 %) and artifact
  uploads read back and compared (20 %); and `--chat-workers` (2) users holding conversations,
  with streamed turns (a fifth of them needing an approval, one in twenty cancelled) and old
  conversations deleted.
- **A noisy neighbour** (`t-noisy`): `--noisy-clients` (60) clients that, for `--noisy-length`
  (600 s) every `--noisy-period` (2,400 s), create slow runs back to back and retry a refusal
  after 100 ms.
- **Rolling retention**, so the database reaches a steady state as a production one does: every
  `--retire-every` (300 s) the runs that finished more than `--retire-after` (300 s) ago are
  checked (their effects against the ledger, their approvals, the driver's decisions), then aged
  two days, with audit events, idle conversations and finished workflow runs of that age; the
  sweep must delete them all (with events, approvals and ledger records) before the next cycle.
- **Chaos**: from `--restart-first` (1,200 s), every `--restart-every` (1,500 s) the Runtime is
  stopped with SIGTERM (`stop -t 45`, `--stop-timeout`) or killed with SIGKILL, in the order `--restart-pattern`
  (`TERM,TERM,KILL`), and started again; PostgreSQL is restarted once at `--postgres-restart-at`
  (0.55) of the duration. Clients retry while the service is unavailable and follow streams again
  with `Last-Event-ID`, as a host would.

## What is recorded (in OUT_DIR)

`windows.jsonl` (per `--window-seconds`, 300: operations, p50/p95/p99 per operation, errors by
class, whether the noisy tenant was flooding), `samples.jsonl` (per `--sample-seconds`, 30: the
Runtime's RSS and peak RSS, open FDs, threads, CPU, asyncio tasks; PostgreSQL connections by
state, database size, live and dead tuples and size per table; Redis memory, keys and clients),
`chaos.jsonl`, `retention.jsonl`, `journal.jsonl` (everything started and every decision),
`summary.json` and `runtime.log`.

## Invariants checked at the end

After the load stops, work in flight finishes and waits expire (`settle`), then:

- **runs**: every run the driver started is in the database (unless retention deleted it after
  it was checked) and is terminal or waits for reconciliation after a kill; none is stuck in a
  working state.
- **approvals**: no approval is pending on a finished run; every decision the driver made is
  recorded; every approval left alone ended its run as `timed_out`.
- **ledger**: no `soak.record` effect happened twice; every effect has its ledger record; every
  completed ledger record has its effect; no record is left `started`.
- **stuck**: no conversation turn or workflow is still working or waiting.
- **retention**: every cycle's checks held (no duplicate effect, no effect without its record, no
  completed record without its effect, no record left `started`, no pending approval on a
  finished run, every decision recorded, every approval left alone timed out); every aged row was
  deleted by the next cycle; no expired artifact is still stored.

The containers are removed with their volumes, and PostgreSQL's WAL is capped
(`max_wal_size=256MB`), so a soak of hours needs a few GB of container disk.
