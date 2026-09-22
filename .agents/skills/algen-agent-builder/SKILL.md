---
name: algen-agent-builder
description: Create or update an Algen-compatible single-agent or multi-agent project that uses Runtime manifests, governance, tests, and Studio-importable packaging.
---

# Build an Algen agent project

Start by choosing the smallest topology that fits: one agent for a single governed conversation, or
a Runtime `WorkflowManifest` for coordination. Scaffolding is available via the CLI:
`algen-agent-runtime new <name> [--template agent|approval|workflow]`. Read `references/project-shape.md`;
for a workflow, also read `references/workflows.md`. Use `$algen-agent-connector` when external tools,
safe starter tools (`algen_agent_runtime.tools.starter`), databases, retrieval, MCP, or remote APIs are part
of the request.

Prefer configuration for provider routing, policies, memory, retrieval, storage, telemetry, and
workflow topology. Keep only domain-specific payload builders, validators, schemas, predicates, and
deterministic handlers in the project's hook provider. Never implement a second scheduler or a
Studio-specific workflow dialect. For automated CI and headless evaluation, use non-interactive workflow
options (`--approve-all`, `--reject-all`, `--answers-file PATH`).

Produce an importable project with secret references, bounded execution, deterministic offline
tests, documented optional dependencies, and a small runnable entry point. A project intended for
Studio must keep its Runtime configuration and `WorkflowManifest` authoritative; Studio discovers
and runs that same contract.

Validate YAML by loading it through the current Runtime model rather than relying on syntax alone.
Run the project smoke test and relevant Runtime tests. Do not publish to a marketplace or deploy
unless the user explicitly requests that external action.

