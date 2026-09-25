# API stability policy

Owner: AlgenAI Maintainers  
Status: Active (0.1.x pre-release)  
Last reviewed: 2026-09-24

Algen Agent Runtime follows Semantic Versioning. The current `0.x` line is pre-release software, so APIs may change as the contracts are validated in real applications. This policy makes those changes reviewable rather than silent.

## Public interfaces

The following are intended public integration surfaces:

- names exported from `algen_agent_runtime`;
- documented HTTP endpoints and event payloads;
- documented agent and runtime configuration fields;
- plugin, tool, provider, framework, storage, retrieval, policy, and verifier contracts explicitly described as extension points;
- persisted schemas and migrations used by supported storage adapters.

Imports from undocumented modules may be needed for advanced integrations during `0.x`, but they are not yet covered by the same compatibility promise.

## Compatibility rules

- Patch releases fix defects without intentionally breaking documented public interfaces.
- Minor `0.x` releases may change experimental interfaces, but must include changelog and upgrade guidance.
- Persisted-data changes require forward migrations and compatibility tests.
- Configuration and HTTP surface removals are documented with direct replacement guidance. During
  the pre-`1.0` line, obsolete compatibility aliases may be removed without retaining executable
  shims.
- Event and error identifiers should remain stable once documented.
- Provider and framework behavior is constrained by the compatibility versions declared in `pyproject.toml` and the tested compatibility matrix.

The project will publish a stricter long-term support and deprecation window before `1.0.0`.
