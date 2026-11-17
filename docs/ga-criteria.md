# General availability (1.0) criteria

Owner: AlgenAI Maintainers  
Status: Active tracking — beta (`0.2.0b4`; `0.2.0b5` in preparation)  
Last reviewed: 2026-11-16

`1.0.0` is a promise, not a feature set: documented public interfaces stay compatible across
`1.x`, upgrades are safe and reversible, and a deployment can be operated against published
service objectives. This checklist tracks what must be true before that promise is made. It builds on
[production readiness](production-readiness.md), whose P0 list is necessary but not sufficient.

## Release path

P0 and conversation streaming ship on the `0.1` line first, in small reviewed batches, while the
Runtime's internal users adopt each alpha. `0.2` marks a frozen public API candidate, so it waits for
production evidence rather than for the feature list.

| Release | Purpose | Exit condition |
| --- | --- | --- |
| `0.1.0a16` | Conversation streaming and approval pauses ([#50](https://github.com/AlgenAI/algen-agent-runtime/issues/50)); P0-6 readiness and lifecycle | Released; internal users start adopting |
| `0.1.0a17` | P0-4 authentication options; P0-3 side-effect operations, each reviewed on its own | Released |
| `0.1.0a18` | Remaining P0 items: network and plugin hardening, resource bounds, policy hardening, persistence drills | Released; all P0 items complete for the single-node scope |
| `0.1.0a19` | Governance fixes found building governed agents ([#83](https://github.com/AlgenAI/algen-agent-runtime/issues/83)), scan evidence ([#80](https://github.com/AlgenAI/algen-agent-runtime/issues/80)), session-scoped tool cache ([#79](https://github.com/AlgenAI/algen-agent-runtime/issues/79)), file attachments ([#82](https://github.com/AlgenAI/algen-agent-runtime/issues/82)); document extractors deprecated ([#81](https://github.com/AlgenAI/algen-agent-runtime/issues/81)) | Released; the extractor deprecation restarts the "no new deprecations" count below |
| `0.1.0a20` | Patch: a toolkit's configuration error keeps its type and message (0.1.0a19 regression) | Released |
| `0.1.0a21` | Recoverable malformed tool calls ([#89](https://github.com/AlgenAI/algen-agent-runtime/issues/89)) and output repair on models that refuse prefill ([#90](https://github.com/AlgenAI/algen-agent-runtime/issues/90)); startup banner; `check --profile standalone` | Released; no new deprecations |
| `0.1.0a22` | Workflow approvals decided by the approval node's status (security fix); synchronous tools off the event loop ([#108](https://github.com/AlgenAI/algen-agent-runtime/issues/108)); approval-argument warnings and digests ([#109](https://github.com/AlgenAI/algen-agent-runtime/issues/109)); adapter scope in the README ([#110](https://github.com/AlgenAI/algen-agent-runtime/issues/110)) | Released; no new deprecations |
| `0.1.0a23` | A workflow tool step needs every approval that gates it (security fix); timed-out synchronous tools keep their concurrency slot ([#114](https://github.com/AlgenAI/algen-agent-runtime/issues/114)); `check` reads a modifiable decision only through its parameters ([#109](https://github.com/AlgenAI/algen-agent-runtime/issues/109)) | Released; no new deprecations |
| `0.2.0b1` | Beta: deprecated APIs removed; optional `server`/`otlp` extras and the namespaced examples package ([#115](https://github.com/AlgenAI/algen-agent-runtime/issues/115)); strict approval binding ([#109](https://github.com/AlgenAI/algen-agent-runtime/issues/109), [#117](https://github.com/AlgenAI/algen-agent-runtime/issues/117), [#127](https://github.com/AlgenAI/algen-agent-runtime/issues/127)) and the other open issues | Released; entry criteria met or waived |
| `0.2.0b2` | Fixes from an independent review of 0.2.0b1 (session ownership, run actors, delegation allowlist, output integrity, policy false positives); approval attribution, client credentials, `core.subagent` from settings. Additive surface; behaviour changes listed in the upgrade notes | Released |
| `0.2.0b3` | Conversation turns survive restarts and deploys; pauses followed end to end (delegated pauses, `core.ask_user`, credentials after recovery); `budget.on_limit: summarize`; typed run errors; migration SQL, `check_schema`, user data export and erasure; public redaction helpers; settable context budget; workflow approval and input routes follow the run actor rule; the host skill and skills installed from the package. Additive surface; behaviour changes listed in the upgrade notes | Released |
| `0.2.0b4` | Security assessment fixes; framework adapter fixes ([#146](https://github.com/AlgenAI/algen-agent-runtime/issues/146)); GA evidence: the deployment scope and adapter support decided, upgrade and rollback drills against `0.1.0a23` and `0.2.0b3`, the [upgrade guide](upgrading.md), the [support and deprecation policy](api-stability.md#support-and-deprecation-policy-1x), and the performance suite with published results ([#118](https://github.com/AlgenAI/algen-agent-runtime/issues/118)) | Public API candidate frozen; only fixes and additive changes |
| `0.2.0b5` | The remaining GA evidence: provider cancellation, golden redacted telemetry, a soak of hours, live provider contracts; run-level metrics, dashboards, alerts and published SLOs; experimental interfaces marked in code; event payload shapes in the API surface; every doc page, skill and example reviewed. Fixes and additive changes only | Every section below except "Stability evidence" is checked |
| `0.2.0b6` … `0.2.0bN` | Only if the stability window finds a defect: fixes only | Every section below except "Stability evidence" stays checked |
| `0.2.0` | Supported pre-1.0 release | No breaking change for the stability window |
| `1.0.0rc1` … | Release candidates | Only fixes; the support and deprecation policy is published |
| `1.0.0` | General availability | Every box below is checked |

Entry criteria for `0.2.0b1`:

- [x] Every P0 item in [production readiness](production-readiness.md) is complete.
- [x] ~~Algen Agent Studio, the HRMS agents and the Traccia azAI service have run the latest `0.1`
      alpha in production for about two weeks.~~ **Waived on 2026-11-11** by the maintainers:
      `0.1.0a23` was tested extensively and runs in Studio production and other agents, one week
      after release. The beta's stability window below still needs the full production period.
- [x] The last two or three alphas introduced no new deprecations (`0.1.0a21` to `0.1.0a23`).
- [x] No internal user still depends on an interface scheduled for removal in `0.2`: Studio's usage
      meter uses `reserve(..., call=...)`; nothing imports `communications` or `database_tool`.
      The HRMS agents still import the document extractors; they move to
      `algen_agent_tools.documents.inspection` before upgrading to `0.2`.

A breaking change after `0.2.0b1` restarts the stability window.

## Scope decision

- [x] Decide and document the GA deployment scope. **Decided on 2026-11-15: single node**, one
      Runtime process per deployment with PostgreSQL (and optionally Redis) stores. Horizontal
      scaling stays experimental in `1.0` unless every item under "Remaining for hardened multi-worker
      or horizontally scaled use" in [production readiness](production-readiness.md) is complete, or
      durable distributed execution is provided through an integration (for example Temporal).
      Documented in [production readiness](production-readiness.md#deployment-scope),
      [operations](operations.md) and [API stability](api-stability.md#experimental-interfaces).
- [x] Decide the supported framework adapters. **Decided on 2026-11-15:** LangGraph and the OpenAI
      Agents SDK are supported; AutoGen is experimental and frozen (its upstream README says the
      project is archived and gets no bug or security fixes; the last `autogen-agentchat` release
      is `0.7.5`, 2025-09-30; the GitHub repository is not yet flagged archived); CrewAI stays
      experimental until it gains real-SDK evidence
      ([framework adapters](framework-adapters.md#support-status)).

## P0: hardened single-node production

- [x] All seven P0 items in [production readiness](production-readiness.md) are complete:
  - [x] 1. Persistence drills against live stores (single node; `0.1.0a18`).
  - [x] 2. Policy distribution and adversarial coverage (`0.1.0a18`).
  - [x] 3. Side-effect reconciliation, operator resolution and compensation (`0.1.0a17`).
  - [x] 4. Authentication options (OIDC/JWKS, service identity, revocation, auth audit) (`0.1.0a17`).
  - [x] 5. Network, plugin and local-execution hardening (`0.1.0a18`).
  - [x] 6. Readiness and lifecycle (`0.1.0a16`).
  - [x] 7. Resource bounds and retention (`0.1.0a18`).
- [x] The [production gate](production-readiness.md#production-gate) has automated evidence,
      including provider cancellation, schema upgrade **and rollback**, and recovery under repeated
      process failure (`0.2.0b5`). Schema upgrade and rollback (against `0.1.0a23` and `0.2.0b3`),
      restart-safe approvals and tool execution, and recovery under repeated process failure: the
      persistence drills (`tests/drills`). Bounded resource use under load and between tenants: the
      performance suite (`tests/perf`). Cross-tenant isolation and authenticated access: the
      adversarial suite (`tests/adversarial`) and the API and authentication tests. Provider
      cancellation: `tests/unit/test_provider_cancellation.py`, `test_run_cancellation.py`,
      `test_tool_cancellation.py` and the drill `tests/drills/test_cancellation.py`
      ([operations](operations.md#cancellation)). Redacted telemetry: the golden suite
      (`tests/telemetry`, in memory, PostgreSQL, Redis and host logging;
      [operations](operations.md#redacted-telemetry)).

## Public API

- [x] The public surface is listed explicitly: exported names, HTTP endpoints, event and error
      identifiers, configuration fields, extension-point protocols and persisted schemas
      ([API stability](api-stability.md)). The `0.2.0b1` baseline is
      [`docs/api/surface.json`](api/surface.json), generated by `scripts/api_surface.py`. Event
      payload shapes are in it since `0.2.0b5` (`event_payloads`).
- [x] Everything marked deprecated is removed: `algen_agent_runtime.communications`,
      `tools.adapters.database_tool`, the `0.1.0a14` `UsageMeter.reserve` form without `call`, and
      the document extractors in `algen_agent_runtime.security` (`0.2.0b1`).
- [x] Experimental interfaces that are not ready are moved out of the public surface or marked
      experimental in code and docs (`0.2.0b5`): horizontal scaling and distributed execution, the
      AutoGen and CrewAI adapters, the analytical graph engine, remote model services and the Jev
      decision client carry `@experimental` / `__experimental__` markers and
      `"stability": "experimental"` in `surface.json` and the settings schema
      ([experimental interfaces](api-stability.md#experimental-interfaces)).
- [x] A long-term support and deprecation policy is published: how long a deprecated interface keeps
      working in `1.x`, which Python versions are supported, and how security fixes are backported
      ([support and deprecation policy](api-stability.md#support-and-deprecation-policy-1x):
      two minor releases and six months; Python 3.12 and 3.13; the latest minor plus the previous
      minor for six months). `SECURITY.md` adopts its supported-versions table at `1.0.0`.
- [x] An API-diff check runs in CI and fails on an unannounced change to the public surface:
      `tests/unit/test_api_surface.py`, in the base test job, prints what changed.

## Upgrades and data

- [x] Every migration has an upgrade test from the previous minor release and a documented rollback
      or restore procedure. The drills (`tests/drills/test_upgrade_rollback.py`) upgrade from and
      roll back to two tags, `v0.1.0a23` (the previous minor's last release, so every `0.2`
      migration from `017` on is applied in the test) and `v0.2.0b3` (the previous release); the
      procedures and a per-migration rollback table are in
      [operations](operations.md#upgrades-and-rollback).
- [x] Checkpoints written by the previous minor release resume on the new one (tested). A run
      `0.1.0a23` (and `0.2.0b3`) paused, in that release's own code, resumes on the current code in
      the upgrade drill, with its approval, ledger and one external effect.
- [x] An upgrade guide covers each `0.x` minor release up to `1.0`: [upgrading](upgrading.md)
      covers `0.1` to `0.2` (each `0.2` beta); a section is added for each later minor.

## Release engineering

- [x] Reproducible dependency lock files for the service image and CI: hash-locked
      `requirements/*.txt` (`scripts/lock-requirements.sh`), used by CI, the release build and the
      image (`0.2.0b4`).
- [x] SBOM and license report published with each release: `sbom.cdx.json` and `licenses.md` on
      the GitHub release (`0.2.0b4`, first published with `v0.2.0b4`).
- [x] Signed tags or provenance attestations for every release artifact: GitHub build provenance
      and PEP 740 attestations (`gh attestation verify`; [releasing](releasing.md)); `v*` tags are
      protected by a ruleset (`0.2.0b4`).
- [x] A container runtime smoke test (start, readiness, one mock run, drain), not only build and
      scan: `scripts/container-smoke.sh` in CI and `scripts/ci-local.sh` (`0.2.0b4`).
- [x] Security rules enforced by tests, not only by review: every HTTP route classified and
      checked for authentication and tenant isolation (`tests/unit/test_api_tenant_matrix.py`),
      every approval status sorted (`tests/unit/test_approval_decisions.py`), tools run only
      through the executor (`tests/unit/test_tool_execution_chokepoint.py`) and the workflows
      audited with zizmor (`scripts/check-workflows.sh`, Security workflow); the rules are in
      AGENTS.md, "Security invariants" (`0.2.0b5`).

## Operations

- [x] Load, soak, fault-injection, crash-recovery and noisy-neighbor tests with published results.
      Crash-recovery and store fault-injection run as persistence drills (`tests/drills`); the
      load and noisy-neighbour tests are in the performance suite (`tests/perf`, marker `perf`);
      the soak drill ran 5,000 items. Results, machine and commit are in
      [operations](operations.md#performance-and-capacity-evidence) (2026-11-15, `0.2.0b3`). A
      six-hour soak of the service image under mixed HTTP load, with restarts, kills and a
      PostgreSQL restart, held every invariant (2026-11-17, `0.2.0b5`,
      [results](operations.md#long-soak-hours)).
- [x] Golden telemetry and redaction tests across model, tool, retrieval, approval, verification and
      failure paths (`tests/telemetry`, `0.2.0b5`): canaries through every run path against every
      sink, golden shape files, PostgreSQL, Redis and host-logging variants
      ([operations](operations.md#redacted-telemetry)).
- [x] Published SLOs, capacity guidance, dashboards, alerts and runbooks for the single-node scope
      (`0.2.0b5`): run-level metrics, Grafana dashboards and Prometheus alert rules in
      `deploy/observability`, SLOs with their measured basis, and a runbook per alert
      ([operations](operations.md#slos-alerts-and-runbooks-single-node)). The targets come from
      laptop measurements; revisit them after the production window.
- [x] Benchmark baselines for latency overhead per step and per tool call
      (`tests/perf/test_overhead.py`; [results](operations.md#overhead-per-step-and-per-tool-call):
      p95 per step 2.1 ms in memory and 34.9 ms on PostgreSQL).

## Documentation

- [x] Every doc page reviewed for the `1.0` scope; stale version references removed (2026-11-16:
      every `docs/*.md` page, the Agent Skills, the example READMEs and `docs/docs.html`;
      historical pages carry banners).
- [x] The comparison in [Who should use it](why-algen-agent-runtime.md) refreshed within the quarter
      before release: versions and capability cells re-checked on 2026-11-16 with dated sources,
      the Algen column at `0.2.0b5`. Refresh it again if `1.0.0` ships after 2027-02-16.
- [x] Studio's Runtime documentation synced for `1.0`: [Studio compatibility](studio-compatibility.md)
      describes Studio's current checks and the settings an imported project declares; Studio's
      own pin and support-range checklist is on its `docs/runtime-0.2-sync` branch (2026-11-16).

## Stability evidence

- [ ] No breaking change for at least four to six weeks on the `0.2` line. The window counts from
      `0.2.0b4` (the last release that changed behaviour); `0.2.0b5` is fixes and additive
      changes.
- [ ] The Runtime's internal users run the same release in production through that window: Algen
      Agent Studio, the HRMS agents and the Traccia azAI service.
- [ ] No open critical or high security issue, and no open data-loss defect. True on 2026-11-16
      (the 0.2.0b3 assessment's findings are fixed in `0.2.0b4`); it must still hold at the end of
      the window.
