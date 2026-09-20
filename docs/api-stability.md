# API stability policy

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
- Configuration fields are not removed silently. Prefer a deprecation period when practical.
- Event and error identifiers should remain stable once documented.
- Provider and framework behavior is constrained by the compatibility versions declared in `pyproject.toml` and the tested compatibility matrix.

## Deprecation process

A deprecation should include a runtime warning where practical, a changelog entry, replacement guidance, and tests for both the old and new path during the compatibility window. Security fixes may require faster removal when retaining behavior would expose users to material risk.

The project will publish a stricter long-term support and deprecation window before `1.0.0`.
