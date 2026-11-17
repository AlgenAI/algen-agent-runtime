# API stability and support policy

Owner: AlgenAI Maintainers  
Status: Active (`0.2` beta line); the support and deprecation policy below applies from `1.0.0`  
Last reviewed: 2026-11-16

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

[`docs/api/surface.json`](api/surface.json) records the surface as of the current release.
`scripts/api_surface.py` generates it from the code. Its keys:

| Key | What it records |
| --- | --- |
| `exports` | Every name in `algen_agent_runtime` and each subpackage's `__all__`, with its signature or model fields; an experimental one also carries `"stability": "experimental"` |
| `http_routes` | The HTTP routes (method and path) |
| `configuration` | Every configuration field (dotted path) and its type |
| `errors` | The exception classes and their bases |
| `events` | The event and audit identifiers the Runtime emits |
| `event_payloads` | The payload shapes of the public events (since `0.2.0b5`), below |
| `check_codes` | The codes `algen-agent-runtime check` reports |
| `protocols` | The `Protocol` extension points and their methods |
| `migrations` | The SQL migrations |
| `experimental` | Every experimental export, protocol and configuration field (since `0.2.0b5`) |

`event_payloads` holds:

- `envelopes`: the fields of `RunEvent`, `ConversationEvent`, `AuditEvent` and
  `FrameworkStreamEvent`, and the keys every workflow event carries (`workflow_id`,
  `workflow_name`, `status`, and `node_id` and `node_kind` for a node's event);
- `run`, `conversation` and `workflow`: for each event type, the keys its `data` can carry, read
  from every place the Runtime emits it. A key is not always present (a payload can differ from
  one emitting place to another), and `*` means the payload can also carry keys the script cannot
  name, such as a model dump or a caller's mapping;
- `sse`: the frames of `GET /v1/runs/{run_id}/events` and
  `GET /v1/conversations/{conversation_id}/events` (`id`, `event` and `data` lines, the
  `stream.overflow` frame and its `data` keys, and the keepalive comment).

Audit event `metadata` keys are not recorded.

`tests/unit/test_api_surface.py` fails when the code and the file differ, and prints the
difference grouped by what it needs (`python scripts/api_surface.py --changes` prints the same):

| Group | Meaning | What the pull request needs |
| --- | --- | --- |
| `removed` | A stable entry is gone | Breaking: an upgrade note; it restarts the stability window |
| `changed` | A stable entry differs, or became experimental | Check compatibility: an incompatible change is breaking |
| `added` | A new stable entry, or a payload that gains keys | A `CHANGELOG.md` line |
| `experimental` | An experimental entry was added, changed or removed | Allowed: a `CHANGELOG.md` line (and an upgrade note for a removal), no deprecation window |

A pull request that changes the surface on purpose regenerates the file
(`python scripts/api_surface.py --update`), commits it with the change, and records the change in
`CHANGELOG.md`; the test fails until it does, whatever the group, so the file always matches the
code. From `0.2.0b1`, later `0.2` betas only add to this file: a removal or an incompatible change
to a stable entry needs an upgrade note and restarts the stability window
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
The docs and the code mark each one as experimental. The following are experimental and stay so
in `1.0` unless this list says otherwise:

| Interface | Why it is experimental | Docs |
| --- | --- | --- |
| Horizontal scaling: more than one Runtime process per database, `distributed_execution`, `DistributedWorker`, the work-item queues and worker leases | The GA deployment scope is a single node; multi-worker recovery, fencing and distributed limits are not complete | [Deployment scope](production-readiness.md#deployment-scope), [remaining for multi-worker use](production-readiness.md#remaining-for-hardened-multi-worker-or-horizontally-scaled-use), [Distributed worker operations](operations.md#distributed-worker-operations-and-storage-validation) |
| The AutoGen AgentChat adapter (`autogen` extra) | Frozen: AutoGen is archived upstream; no new features, and it may be removed in a minor release | [Framework adapters](framework-adapters.md#support-status) |
| The CrewAI adapter (`crewai` extra) | Tested only with mock crews, not the real SDK | [Framework adapters](framework-adapters.md#support-status) |
| The analytical graph engine (since `0.2.0b5`): `AnalyticalGraphEngine`, its graph types and stores, `analytical_execution`, the plugin `analytical_nodes` registry, and `SemanticQueryGraphPlanner` | No application runs it in production (only a reference example), and analytical dispatch is not integrated into the service lifecycle. Analytical methods, `AnalyticalResult`, the semantic layer and query governance are stable: applications use them in production | [Analytical runtime](analytical-runtime.md#typed-analytical-graphs) |
| Remote model services (since `0.2.0b5`): `algen_agent_runtime.model_services` and the plugin `model_services` registry | No application uses them yet; the HTTP client, health and drift contracts are tested against local fakes only | [Analytical runtime](analytical-runtime.md) |
| The TypeSafe Jev decision client (since `0.2.0b5`): `algen_agent_runtime.decisions` | A client for a third-party service, tested against a mocked HTTP API only; no application uses it yet | [README](../README.md) |

The LangGraph and OpenAI Agents SDK adapters are supported: they are tested against the real SDKs
and follow the deprecation window. Inside a framework, only the outer call and Runtime tools handed
over through `GovernedToolBridge` are governed by the Runtime; the framework's own model and tool
calls are not, whatever the adapter's status.

The rows marked "since `0.2.0b5`" were reviewed with the rest of the public surface before
`0.2.0`; they were never covered by a stability promise (the `0.x` rules apply), so marking them
changes no behaviour. Everything else in the surface was confirmed stable, among it learned memory,
context evaluations, the semantic layer, analytical methods, query governance, the agent UI
generator, and the LangGraph and OpenAI Agents SDK adapters.

### How experimental interfaces are marked

Since `0.2.0b5` the code marks them too, through `algen_agent_runtime.stability`. The marker is
for people and tools only: it emits no warning and changes no behaviour.

- **Classes and functions** carry `@experimental("<reason>")`, and their docstring starts with
  `Experimental:`.
- **Packages** set a module-level `__experimental__ = "<reason>"`: everything they export is
  experimental (`algen_agent_runtime.distributed`, `algen_agent_runtime.model_services`,
  `algen_agent_runtime.decisions`).
- **Configuration fields** carry `"stability": "experimental"` in the settings JSON schema
  (`AppSettings.model_json_schema()`), and the fields under them are experimental too.
- `is_experimental(value)` and `experimental_reason(value)` answer for a class, function or
  module, so a host (a settings editor, a capability inspector) can label experimental interfaces
  without keeping its own list:

```python
from algen_agent_runtime.frameworks import AutoGenAdapter, LangGraphAdapter
from algen_agent_runtime.stability import experimental_reason, is_experimental

is_experimental(LangGraphAdapter)  # False
experimental_reason(AutoGenAdapter)  # "AutoGen is archived upstream: the adapter is frozen, ..."
```

`scripts/api_surface.py` reads these markers into [`surface.json`](api/surface.json), and
`tests/unit/test_api_surface.py` checks that every experimental export and protocol has an
`Experimental:` docstring and is named below.

Marked in code (`0.2.0b5`):

| Area | Exports and protocols | Configuration |
| --- | --- | --- |
| Horizontal scaling | `DistributedWorker`, `InMemoryWorkQueue`, `PostgresWorkQueue`, `WorkItem`, `WorkQueue`, `WorkStatus` | `distributed_execution`, `maintenance.work_item_retention_days` |
| Framework adapters | `AutoGenAdapter`, `CrewAIAdapter` | |
| Analytical graph engine | `AnalyticalGraphEngine`, `AnalyticalNodeRegistry`, `InMemoryAnalyticalGraphStore`, `PostgresAnalyticalGraphStore`, `builtin_handlers`, `AnalyticalGraphStore`, `AnalyticalNodeHandler`, `SemanticQueryGraphPlanner`, `SemanticQueryTask`; also the graph types `AnalyticalGraph`, `AnalyticalNode`, `AnalyticalNodeKind`, `AnalyticalGraphState`, `NodeExecution`, `NodeStatus`, `GraphStatus`, `NodeRetryPolicy`, `NodeCachePolicy`, `NodeBudget`, `ResultReference` | `analytical_execution`, `maintenance.analytical_graph_retention_days` |
| Remote model services | `DeterministicModelService`, `DriftState`, `HTTPModelService`, `ModelService`, `ModelServiceHealth`, `ModelServiceKind`, `ModelServiceManifest`, `ModelServiceRegistry`, `ModelServiceRequest`, `ModelServiceResponse` | |
| Jev decisions | `JevAnswer`, `JevDecisionClient`, `JevDecisionError`, `JevQuestion`, `JevRequest`, `JevResponse`, `JevSettings`, `JevUsage` | |

The database tables behind experimental features (work items, analytical graphs) are persisted
schemas: like every migration they stay additive, whatever the feature's status.
