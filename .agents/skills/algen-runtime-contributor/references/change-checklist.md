# Runtime change checklist

Use the applicable sections; do not force irrelevant work into a small change.

## Security

- Does the change add an HTTP route? Classify it in `ROUTES` of
  `tests/unit/test_api_tenant_matrix.py` (public, platform, tenant or resource) with a valid body
  in `BODIES`; the test then checks authentication and tenant isolation for it.
- Does it add a way to run tools, or decide an approval? It calls `ToolExecutor.execute` with the
  approval that covers the call (`tests/unit/test_tool_execution_chokepoint.py`), and compares a
  decision with the set that approves, never with `rejected`.
- Does new text reach a user (an event, a streamed delta, a message)? It passes the run's output
  policies.
- Does it check one value and then use another (a signed file and an imported one, validated SQL
  and executed SQL)?
- Does it change `.github/` or `requirements/`? `scripts/check-workflows.sh` passes.
- The pull request names the rules in AGENTS.md, "Security invariants", the change touches.

## Boundary and compatibility

- Is this a reusable Runtime capability rather than Studio or marketplace behavior?
- Does it change a public import, YAML contract, event, error code, or persisted schema?
- If it changes `WorkflowManifest`, are manifest validation, execution validation, Studio
  consumption, examples, agent-builder guidance, and the changelog updated together?
- If it adds a registry, a name-valued manifest field, or changes what `build_container` registers,
  are `RuntimePluginContext`, `describe_components` and its container-parity test
  (`tests/unit/test_describe.py`), the startup check (`orchestration/validation.py`),
  `docs/plugins.md`, the agent-builder skill, and the changelog updated together?
- Does every project under `algen_agent_runtime_examples/` still pass `algen-agent-runtime check`
  (`tests/unit/test_check.py`)? A new example registers its code components from a declared plugin,
  gets a row in `algen_agent_runtime_examples/README.md` (`tests/unit/test_examples_index.py`), an
  offline entry in `RUNS` of `tests/integration/test_all_examples_run.py`, a test of what it
  demonstrates, and, when the wheel ships it, a file in `REQUIRED` of `scripts/check_dist.py` and a
  run in `scripts/smoke-install.sh`.
- Does every new or changed tool pass `assert_tool_contract`, and does a change to a preset in
  `tools/authoring.py` keep the rule that writes are keyed and single-attempt?
- If compatibility changes, is there an upgrade note, migration path, and changelog entry?
- Does it change the public surface (an export, route, configuration field, error, event or its
  payload keys, protocol, migration)? Run `python scripts/api_surface.py --changes`, then
  `--update`, and commit `docs/api/surface.json` with the change. `removed` and incompatible
  `changed` entries are breaking: an upgrade note, and they restart the stability window.
  `experimental` entries need only a changelog line.
- Is the new interface not ready for a compatibility promise (no production user, no real-backend
  test, a deployment layout outside the supported scope)? Mark it experimental:
  `@experimental("<reason>")` from `algen_agent_runtime.stability` (or
  `__experimental__ = "<reason>"` for a whole package,
  `json_schema_extra=EXPERIMENTAL_FIELD` for a settings field), a docstring that starts with
  `Experimental:`, and a row in `docs/api-stability.md`, "Experimental interfaces";
  `tests/unit/test_api_surface.py` checks all three. Marking emits no warning. Making a stable
  entry experimental is a `changed` (breaking) entry.
- Could a host application that embeds the Runtime already have built this capability itself:
  conversation or approval state, event handling, recovery, steering, model calls, metering,
  redaction, guards, storage? Then add a row to the
  table in `.agents/skills/algen-runtime-host/references/host-capabilities.md` (the API, the
  release that adds it, and when a host should keep its own), and an entry under
  `### You can now remove` in the `[Unreleased]` section of `CHANGELOG.md`. Check the known hosts
  (Algen Studio; fly91-hrms; the Traccia assistant service, `traccia-assistant-service`, whose
  assistant is Traccia's "azai", not Algen Studio's azAI) for their own version, and tell the
  maintainer which ones to open an issue in, with the code to delete; never open issues or pull
  requests in another repository yourself.
- Does the change alter how to build on the Runtime (a public API, configuration field, extra,
  example name, removal or behaviour)? Update the affected skills in `.agents/skills/` in the same
  change, update the Markdown docs each affected reader uses and `docs/docs.html` (with
  `data-version-min` on the section), and run `python scripts/sync_docs_skills.py`. Every "since"
  version comes from the git tags (`git grep <name> <tag>`). A new user-facing skill is added to
  `SKILLS` (and `SINCE`) there, to `PACKAGED_SKILLS` in `algen_agent_runtime/_coding_skills.py`,
  to the wheel's `force-include` in `pyproject.toml`, to the skill checks in
  `scripts/check_dist.py` and to the README's "AI coding skills" list.
- Does it change the public surface? Regenerate `docs/api/surface.json`
  (`python scripts/api_surface.py --update`); from `0.2.0b1` later `0.2` betas only add to it.
- Can the previous release still read what this version stores during a rollback? New fields
  stay out of stored run state, checkpoints and manifest fingerprints while empty or unset.
- For durable workflows, are tenant isolation, optimistic version conflicts, manifest fingerprints,
  pause/resume, deadline suspension, recovery opt-in, and interrupted side effects tested?
- For workflow approvals, are authenticated reviewer identity, approve/modify/reject outcomes,
  parameter-schema validation, expiry, rejection propagation, checkpoint durability, event-content
  minimization, and invalid-decision deadline behavior tested?
- For child workflows, are exact-version registry resolution, typed input/canonical output,
  correlation and lineage, cycle/depth limits, nested pause/decision propagation, checkpoint-first
  dispatch, bounded parent concurrency, pre-child-creation failure, and recovery without duplicate
  children tested?
- Does core remain usable without a new optional integration installed?
- For a new model call site, does it go through `ModelRouter` with a `meter_call` built by
  `_meter_call`, so the usage meter sees it with its run context?
- For changes to the tool-call path, does any guard or run-scoped state live in `RunState` (so it
  survives a pause, restart and resume), and does a checkpoint without the new fields still load?
  Are refusals and held calls finished through `_finish_tool_call_without_result`?
- For changes that touch `ToolContext`, scheduling or run state, do caller credentials stay out of
  `RunState`, stores, events, audits, spans, logs, caches and exception text, and are they cleared
  when the run pauses or ends (`tests/unit/test_tool_credentials.py`)?
- For artifact changes, are staged pre-run inputs, checksums, lifecycle state, expiry, tenant-scoped
  list/read/delete, PostgreSQL migration, and large-object boundaries covered?
- For object storage, are partial failure compensation, retry-safe deletion, encryption, checksum
  verification, opaque tenant keys, credential discovery, scanner compare-and-set, and audit evidence
  covered without requiring the optional SDK in core installs?
- For email changes, are header injection, recipient and attachment bounds, BCC privacy, TLS,
  artifact-only attachments, stable idempotency, content-minimal receipts/errors, provider refusal,
  and ambiguous delivery failure tested?

## Security and operations

- Preserve tenant isolation and authorization at every read and write boundary.
- Resolve secrets through `env://` or `secret://`; never serialize values into manifests or events.
- Classify tool side effects and idempotency accurately.
- Default interrupted workflow nodes to fail closed; never infer retry safety from node kind alone.
- Bound retries, repairs, loops, fan-out, payload sizes, concurrency, and network access.
- Avoid telemetry content by default; redact credentials and sensitive payloads.
- Keep workflow approval review paths and parameters explicitly secret-free; approval events must not
  expose review values, parameters, or operator comments. Hidden values use `hidden_value_marker`
  (`[REDACTED len=N sha256=...]`), and a decision is checked with `check_decision` (approved,
  rejected or modified only).
- Acting on another user's data: artifacts are visible to their owner, their run's user or
  `artifacts:admin`; runs, sessions and conversations to their user. Anything shared by every
  tenant (the agent registry) needs a platform scope (`agents:admin`).
- Delegation must not widen what a run may do: a delegate's tools and permissions stay within its
  parent's, and its usage counts against the parent's budget.
- Streamed output passes the same policies as the final answer (`runtime/stream_guard.py`).
- Plugin loading: Enforce `PluginLoader` with `trusted_plugin_prefixes` checked for every reference
  before any import; keep `register(context)` free of network calls and secrets.
- Dynamic hook loading: Enforce `WorkflowHookLoader` with explicit module allowlists (`allowed_modules`)
  before import; verify callable shape, API version compatibility (`__api_version__`), and `WorkflowHookRegistry`
  return type; never serialize code or secrets in audit metadata.
- Outbound network requests: Enforce `NetworkSecurityPolicy` / `SafeHttpClient` against SSRF, DNS rebinding,
  private/internal IP ranges (IPv4/IPv6 loopback, link-local, RFC 1918, carrier-grade NAT), and redirect chains.
- Rate limiting and quotas: Apply tenant-scoped rate limiting (`RateLimiter`, sliding-window Redis/memory)
  and propagate standard rate limit headers on 429 errors.
- Filesystem safety: Ensure filesystem tools (`algen_agent_runtime.tools.starter.fs`) strictly validate sandbox
  roots against traversal, symlink escapes, and directory breakouts.

## Evidence

- Unit tests: deterministic contracts and failure behavior (e.g., Redis run/memory stores, in-memory queues).
- Contract tests: provider, tool, store, retrieval, or framework compatibility.
- Live provider contracts (`tests/live`, marker `live`): a change to a built-in provider class, or
  a new one (add a `Target` to `tests/live/live_support.py`), passes the suite offline with
  `ALGEN_LIVE_FAKE=1`; a release is preceded by a maintainer run with real keys (`ALGEN_LIVE=1`,
  see `docs/releasing.md`, "Live provider contracts"). Never add vendor keys to CI.
- Integration tests: several Runtime components working together, marked with `integration`, `postgres`, or `redis`.
- Distributed worker tests: worker fencing, lease renewal, stale lease revocation (`ConflictError`), retry bounds, and cancellation.
- Container integration: `scripts/integration-tests.sh` starts throwaway `postgres:17-alpine` and `redis:7-alpine` containers (docker or podman), runs pytest against them and removes them. Run it before every commit, never against a long-lived database (see `AGENTS.md`).
- End-to-end tests: public API or representative application path.
- Golden telemetry (`tests/telemetry`, 0.2.0b5): canary credentials and personal data through every
  run path, checked against every sink (events, SSE, audits, spans, metrics, logs, Traccia, meter
  calls, store rows; PostgreSQL in `test_golden_telemetry_postgres.py`), with each sink's shape
  pinned in `tests/telemetry/golden/*.json`. A change to what the Runtime records fails it: if the
  change is intended, regenerate with
  `ALGEN_UPDATE_GOLDEN=1 python -m pytest -q -p no:cacheprovider tests/telemetry`, review the diff
  and say in the PR what hosts and dashboards will see. A new path or sink gets a scenario there.
- Paid or external networked checks use the `live` marker and are never required for ordinary pull requests.

## Quality gates and completion

```bash
ruff format --check src tests algen_agent_runtime_examples
ruff check src tests algen_agent_runtime_examples
mypy src/algen_agent_runtime
pytest -q -p no:cacheprovider
python scripts/sync_docs_skills.py --check   # docs page in step with the skills
scripts/ci-local.sh              # sdist, wheel from the sdist, dist checks, clean install, Linux base CI
scripts/integration-tests.sh     # real PostgreSQL and Redis, throwaway containers
```

Before raising a pull request, also smoke-test the latest `algen-agent-tools` release against a
wheel built from the branch, and run its test suite there (`AGENTS.md`, "Compatibility with
algen-agent-tools"). Report the results in the PR's test plan.

Use `make lint`, `make typecheck`, and `make test` when the Makefile is the better local entry point.
Provide a structured, conventional Git commit message summarizing the verified changes upon completion.
Every commit needs a DCO `Signed-off-by:` trailer (`git commit -s`).
