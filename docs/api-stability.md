# API stability and support policy

Owner: AlgenAI Maintainers  
Status: Active (`0.2` beta line); the support and deprecation policy below applies from `1.0.0`  
Last reviewed: 2026-11-15

Algen Agent Runtime follows Semantic Versioning. The current `0.x` line is pre-release software, so APIs may change as the contracts are validated in real applications. This policy makes those changes reviewable rather than silent, and says what `1.x` will promise.

## Public interfaces

The following are intended public integration surfaces:

- names exported from `algen_agent_runtime`;
- documented HTTP endpoints and event payloads;
- documented agent and runtime configuration fields;
- plugin, tool, provider, framework, storage, retrieval, policy, and verifier contracts explicitly described as extension points;
- persisted schemas and migrations used by supported storage adapters.

Imports from undocumented modules may be needed for advanced integrations during `0.x`, but they are not yet covered by the same compatibility promise.

## The recorded surface

[`docs/api/surface.json`](api/surface.json) records the surface as of the current release: every
name in `algen_agent_runtime` and each subpackage's `__all__` with its signature or model fields,
the HTTP routes, every configuration field and its type, the exception classes, the event and audit
identifiers, the codes `check` reports, the `Protocol` extension points and the migrations.
`scripts/api_surface.py` generates it from the code.

`tests/unit/test_api_surface.py` fails when the code and the file differ, and prints the
difference. A pull request that changes the surface on purpose regenerates the file
(`python scripts/api_surface.py --update`), commits it with the change, and records the change in
`CHANGELOG.md`. From `0.2.0b1`, later `0.2` betas only add to this file: a removal or an
incompatible change needs an upgrade note and restarts the stability window
([GA criteria](ga-criteria.md)).

## Compatibility rules during `0.x`

- Patch releases fix defects without intentionally breaking documented public interfaces.
- Minor `0.x` releases may change experimental interfaces, but must include changelog and upgrade guidance ([upgrading](upgrading.md)).
- Persisted-data changes require forward migrations and compatibility tests.
- Configuration and HTTP surface removals are documented with direct replacement guidance. During
  the pre-`1.0` line, obsolete compatibility aliases may be removed without retaining executable
  shims.
- Event and error identifiers should remain stable once documented.
- Provider and framework behavior is constrained by the compatibility versions declared in `pyproject.toml` and the tested compatibility matrix.

## Support and deprecation policy (`1.x`)

This is the policy the Runtime adopts at `1.0.0`. Until then the `0.x` rules above apply, and
[SECURITY.md](../SECURITY.md) says which versions receive security fixes today.

### Deprecation window

- A stable public interface (above) is removed or changed incompatibly only in a new major
  release.
- Before that, it is deprecated: it keeps working, warns (`DeprecationWarning` in Python, a
  `Deprecation` response header on an HTTP route, a `check` warning for a configuration field),
  and the changelog's "Deprecated" section and the docs name its replacement.
- A deprecated interface keeps working for **at least two minor releases and at least six months**
  after the first release that deprecates it, whichever is longer.
- A security fix may shorten the window when keeping the old behaviour would leave users exposed.
  The release notes then say so, as `0.1.0a19` did for `requires_approval`.
- Persisted data is never deprecated away: every `1.x` release reads what any earlier `1.x`
  release wrote, migrations stay additive, and rolling back to the previous minor release on a
  migrated database is supported and tested ([upgrading](upgrading.md#rolling-back)).

### Supported Python versions

- Python **3.12 and 3.13**, the versions CI tests and the package classifiers list.
- A new Python version is added in a minor release once the core and every extra the project
  tests install and pass on it. An optional extra whose upstream does not yet support a version is
  documented as such.
- A Python version is dropped only in a minor release, announced at least one minor release (and
  three months) ahead, and never before its upstream end of life.

### Security fixes and backports

- Security fixes ship in a patch release of the **latest minor** line.
- The **previous minor** line receives security fixes for **six months** after the next minor
  release ships.
- Older lines receive none; upgrade to a supported line. Advisories (GitHub security advisories)
  name the affected and fixed versions of every supported line.
- Pre-release lines (alphas, betas, release candidates) receive fixes only through their next
  pre-release or the final release.

Proposed supported-versions table for [SECURITY.md](../SECURITY.md) at `1.0.0` (the maintainers
own that file):

| Version | Security fixes |
| --- | --- |
| Latest `1.x` minor | Yes |
| Previous `1.x` minor | Yes, for six months after the next minor ships |
| Older `1.x` minors | No |
| `0.x` pre-releases | No, after `1.0.0` |

## Experimental interfaces

An experimental interface is documented and usable but outside the compatibility promise: it can
change or go away in any minor release, with an upgrade note but without a deprecation window.
The docs mark each one as experimental. The following are experimental and stay so in `1.0`
unless this list says otherwise:

| Interface | Why it is experimental | Docs |
| --- | --- | --- |
| Horizontal scaling: more than one Runtime process per database, `distributed_execution`, `DistributedWorker`, the work-item queues and worker leases | The GA deployment scope is a single node; multi-worker recovery, fencing and distributed limits are not complete | [Deployment scope](production-readiness.md#deployment-scope), [remaining for multi-worker use](production-readiness.md#remaining-for-hardened-multi-worker-or-horizontally-scaled-use), [Distributed worker operations](operations.md#distributed-worker-operations-and-storage-validation) |
| The AutoGen AgentChat adapter (`autogen` extra) | Frozen: AutoGen is archived upstream; no new features, and it may be removed in a minor release | [Framework adapters](framework-adapters.md#support-status) |
| The CrewAI adapter (`crewai` extra) | Tested only with mock crews, not the real SDK | [Framework adapters](framework-adapters.md#support-status) |

The LangGraph and OpenAI Agents SDK adapters are supported: they are tested against the real SDKs
and follow the deprecation window. Inside a framework, only the outer call and Runtime tools handed
over through `GovernedToolBridge` are governed by the Runtime; the framework's own model and tool
calls are not, whatever the adapter's status.
