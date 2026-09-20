# Changelog

All notable changes to Algen Agent Runtime will be documented in this file.
The project follows [Semantic Versioning](https://semver.org/) while public
contracts are explicitly marked experimental during the `0.x` series.

## [Unreleased]

### Added

- Repository-scoped contributor, extension, agent-building, and connector skills for coding agents.
- A shared Algen ecosystem identity with light/dark Runtime logo and README banner assets, plus
  documented Runtime, Studio, and Marketplace product boundaries.
- Studio-importable Runtime examples shipped with their Python hook providers and configuration in
  source and wheel distributions.
- Runnable cloud chat, typed capability, human checkpoint, governed research, LangGraph adapter,
  multi-agent fan-out, evaluation gate, customer support, incident response, and invoice exception
  examples.
- Runtime DAG manifests for the responsible hiring and teaching-assistant case studies.
- Deterministic configuration, hook-loading, workflow execution, and checkpoint regression coverage
  for the example portfolio.
- Initial open-source governance, security, contribution, and release policies.
- PyPI metadata, deterministic build contents, and clean-install CI checks.
- Public-ready README with an offline quickstart, support matrix, architecture overview, and security boundaries.
- API stability and release-process documentation.
- Dedicated PyPI Trusted Publishing, CodeQL, and pull-request dependency-review workflows.
- Separation of the private Algen Agent Studio application from the Runtime distribution.
- Runtime-owned multi-agent workflow manifests and an embedded DAG executor with agent and
  deterministic-handler nodes, conditional execution, clarification limits, bounded validation
  repair, capped dynamic fan-out, correlated child runs, and lifecycle events.
- Application hook registries for workflow payloads, request metadata, structured-output schemas,
  domain validation, and deterministic service execution.
- Runtime predicate and join nodes, declarative input templates, bounded agent loops, deterministic
  ready-node ordering, conditional-branch convergence, host-provided agent execution, and portable
  application hook-provider references.
- Typed, secret-free workflow resource references for documenting node connections to tools, query
  sources, retrieval indexes, memory, caches, stores, telemetry, and application services.

## [0.1.0a1] - Unreleased

Initial public alpha candidate. This version is intended for evaluation and
contribution and is not a production-readiness claim. Publication requires the
owner-controlled legal, history, repository-protection, and Trusted Publishing
checks documented in the readiness plan.

[Unreleased]: https://github.com/AlgenAI/algen-agent-runtime/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/AlgenAI/algen-agent-runtime/releases/tag/v0.1.0a1
