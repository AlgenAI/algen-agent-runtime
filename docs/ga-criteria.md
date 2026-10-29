# General availability (1.0) criteria

Owner: AlgenAI Maintainers  
Status: Active tracking — pre-release (`0.1.0a17`)  
Last reviewed: 2026-10-28

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
| `0.1.0a18`+ | Remaining P0 items: network and plugin hardening, resource bounds, policy hardening, persistence drills | All P0 items complete |
| `0.2.0b1` … `0.2.0bN` | Beta: deprecated APIs removed, public API candidate frozen | Every section below except "Stability evidence" is checked |
| `0.2.0` | Supported pre-1.0 release | No breaking change for the stability window |
| `1.0.0rc1` … | Release candidates | Only fixes; the support and deprecation policy is published |
| `1.0.0` | General availability | Every box below is checked |

Entry criteria for `0.2.0b1`:

- [ ] Every P0 item in [production readiness](production-readiness.md) is complete.
- [ ] Algen Agent Studio, the HRMS agents and the Traccia azAI service have run the latest `0.1` alpha
      in production for about two weeks.
- [ ] The last two or three alphas introduced no new deprecations.
- [ ] No internal user still depends on an interface scheduled for removal in `0.2` (Studio's usage
      meter uses `reserve(..., call=...)`; nothing imports `communications` or `database_tool`).

A breaking change after `0.2.0b1` restarts the stability window.

## Scope decision

- [ ] Decide and document the GA deployment scope. The recommended scope is **single node**: one
      Runtime process per deployment with PostgreSQL (and optionally Redis) stores. Horizontal
      scaling stays experimental in `1.0` unless every item under "Remaining for hardened multi-worker
      or horizontally scaled use" in [production readiness](production-readiness.md) is complete, or
      durable distributed execution is provided through an integration (for example Temporal).
- [ ] Decide the supported framework adapters. LangGraph and the OpenAI Agents SDK are supported
      today; AutoGen is archived upstream and should be removed or frozen; CrewAI stays experimental
      unless it gains real-SDK evidence.

## P0: hardened single-node production

- [ ] All seven P0 items in [production readiness](production-readiness.md) are complete:
  - [x] 1. Persistence drills against live stores (single node; unreleased).
  - [ ] 2. Policy distribution and adversarial coverage.
  - [x] 3. Side-effect reconciliation, operator resolution and compensation (`0.1.0a17`).
  - [x] 4. Authentication options (OIDC/JWKS, service identity, revocation, auth audit) (`0.1.0a17`).
  - [ ] 5. Network, plugin and local-execution hardening.
  - [x] 6. Readiness and lifecycle (`0.1.0a16`).
  - [ ] 7. Resource bounds and retention.
- [ ] The [production gate](production-readiness.md#production-gate) has automated evidence,
      including provider cancellation, schema upgrade **and rollback**, and recovery under repeated
      process failure. Schema upgrade and rollback (against `0.1.0a17`), restart-safe approvals and
      tool execution, and recovery under repeated process failure have evidence in the persistence
      drills (`tests/drills`); the rest of the gate does not yet.

## Public API

- [ ] The public surface is listed explicitly: exported names, HTTP endpoints, event and error
      identifiers, configuration fields, extension-point protocols and persisted schemas
      ([API stability](api-stability.md)).
- [ ] Everything marked deprecated is removed: `algen_agent_runtime.communications`,
      `tools.adapters.database_tool`, and the `0.1.0a14` `UsageMeter.reserve` form without `call`.
- [ ] Experimental interfaces that are not ready are moved out of the public surface or marked
      experimental in code and docs.
- [ ] A long-term support and deprecation policy is published: how long a deprecated interface keeps
      working in `1.x`, which Python versions are supported, and how security fixes are backported.
- [ ] An API-diff check runs in CI and fails on an unannounced change to the public surface.

## Upgrades and data

- [ ] Every migration has an upgrade test from the previous minor release and a documented rollback
      or restore procedure. The drills test upgrade from and rollback to the previous release's tag
      (`ALGEN_DRILL_PREVIOUS_RELEASE`, today `v0.1.0a17`); the procedures are in
      [operations](operations.md#upgrades-and-rollback). Pointing them at the previous minor
      release is left for `0.2`.
- [ ] Checkpoints written by the previous minor release resume on the new one (tested). A run
      `0.1.0a17` paused resumes on the current code in the upgrade drill.
- [ ] An upgrade guide covers each `0.x` minor release up to `1.0`.

## Release engineering

- [ ] Reproducible dependency lock files for the service image and CI.
- [ ] SBOM and license report published with each release.
- [ ] Signed tags or provenance attestations for every release artifact.
- [ ] A container runtime smoke test (start, readiness, one mock run, drain), not only build and scan.

## Operations

- [ ] Load, soak, fault-injection, crash-recovery and noisy-neighbor tests with published results.
      Crash-recovery, store fault-injection and a bounded soak exist as persistence drills, with a
      first local result in [operations](operations.md#running-the-drills); load and
      noisy-neighbor tests and a published long soak do not.
- [ ] Golden telemetry and redaction tests across model, tool, retrieval, approval, verification and
      failure paths.
- [ ] Published SLOs, capacity guidance, dashboards, alerts and runbooks for the single-node scope.
- [ ] Benchmark baselines for latency overhead per step and per tool call.

## Documentation

- [ ] Every doc page reviewed for the `1.0` scope; stale version references removed.
- [ ] The comparison in [Who should use it](why-algen-agent-runtime.md) refreshed within the quarter
      before release.
- [ ] Studio's Runtime documentation synced for `1.0`.

## Stability evidence

- [ ] No breaking change for at least four to six weeks on the `0.2` line.
- [ ] The Runtime's internal users run the same release in production through that window: Algen
      Agent Studio, the HRMS agents and the Traccia azAI service.
- [ ] No open critical or high security issue, and no open data-loss defect.
