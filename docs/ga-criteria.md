# General availability (1.0) criteria

Owner: AlgenAI Maintainers  
Status: Active tracking — pre-release (`0.1.0a15`)  
Last reviewed: 2026-10-28

`1.0.0` is a promise, not a feature set: documented public interfaces stay compatible across
`1.x`, upgrades are safe and reversible, and a deployment can be operated against published
service objectives. This checklist tracks what must be true before that promise is made. It builds on
[production readiness](production-readiness.md), whose P0 list is necessary but not sufficient.

## Release path

| Release | Purpose | Exit condition |
| --- | --- | --- |
| `0.1.0aN` | Feature work on the current line | — |
| `0.2.0b1` … `0.2.0bN` | Beta: P0 items, conversation streaming and approval pauses ([#50](https://github.com/AlgenAI/algen-agent-runtime/issues/50)), deprecated APIs removed, public API candidate frozen | Every section below except "Stability evidence" is checked |
| `0.2.0` | Supported pre-1.0 release | No breaking change for the stability window |
| `1.0.0rc1` … | Release candidates | Only fixes; the support and deprecation policy is published |
| `1.0.0` | General availability | Every box below is checked |

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
  - [ ] 1. Persistence drills against live stores.
  - [ ] 2. Policy distribution and adversarial coverage.
  - [ ] 3. Side-effect reconciliation, operator resolution and compensation.
  - [ ] 4. Authentication options (OIDC/JWKS, service identity, revocation, auth audit).
  - [ ] 5. Network, plugin and local-execution hardening.
  - [ ] 6. Readiness and lifecycle.
  - [ ] 7. Resource bounds and retention.
- [ ] The [production gate](production-readiness.md#production-gate) has automated evidence,
      including provider cancellation, schema upgrade **and rollback**, and recovery under repeated
      process failure.

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
      or restore procedure.
- [ ] Checkpoints written by the previous minor release resume on the new one (tested).
- [ ] An upgrade guide covers each `0.x` minor release up to `1.0`.

## Release engineering

- [ ] Reproducible dependency lock files for the service image and CI.
- [ ] SBOM and license report published with each release.
- [ ] Signed tags or provenance attestations for every release artifact.
- [ ] A container runtime smoke test (start, readiness, one mock run, drain), not only build and scan.

## Operations

- [ ] Load, soak, fault-injection, crash-recovery and noisy-neighbor tests with published results.
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
