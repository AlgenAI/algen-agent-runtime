# Who should use Algen Agent Runtime—and why

Owner: AlgenAI Maintainers  
Status: Informational  
Last reviewed: 2026-09-22  

## Short answer

Use Algen Agent Runtime when an organization needs the same security, audit, approval, routing, and
observability rules across several agents, model providers, tools, or orchestration frameworks. Do not
adopt it merely to get a chatbot or basic tool-calling loop running.

Algen Agent Runtime is a provider-neutral reference runtime and interoperability layer. It standardizes the
operational envelope around an agent without forcing every team to implement reasoning with an
Algen Agent Runtime-specific abstraction.

## Good fits

### Enterprise AI platform teams

Platform teams supporting agents owned by many product groups need common run identifiers, tenant
isolation, model and tool allowlists, budgets, audit events, approval semantics, redaction, and
telemetry. Algen Agent Runtime centralizes those contracts while domain behavior remains in configuration,
tools, retrieval data, workflows, and plugins.

### Teams operating multiple model providers

Use it when agents must move between OpenAI, Azure OpenAI, Anthropic, Mistral, DeepSeek, Ollama, or
Hugging Face without rewriting orchestration. Capability discovery prevents agents from silently
requesting tools, JSON Schema, images, or streaming from a model that cannot provide them.

### Regulated or side-effecting workflows

Agents changing records, executing operational actions, or accessing sensitive sources need more than
prompt instructions. Algen Agent Runtime distinguishes read-only, externally visible, and destructive tools;
applies authorization and policy checks; creates redacted approvals; and records audit events.

### Traccia integrations

Algen Agent Runtime provides a consistent execution and telemetry vocabulary for Traccia-managed
solutions and acts as the reference implementation for Traccia instrumentation. External LangGraph,
OpenAI Agents SDK, AutoGen, and CrewAI graphs/agents can be registered via the trusted
Application Registration pattern (`build_container(framework_adapters=(...))`), wrapping the outer
execution in standard tenant, timeout, and trace envelopes (see [Framework adapters](framework-adapters.md)).

### Reusable agent products

Teams delivering substantially different agents can reuse the runtime while keeping domain logic out
of the core. Versioned definitions, deterministic providers, typed tools, and pluggable storage reduce
the operational code copied into every product.

## Poor fits

Algen Agent Runtime is probably unnecessary when:

- One small application uses one model, no sensitive data, and one or two read-only tools.
- A managed agent platform is sufficient and provider portability is unimportant.
- An existing framework already meets every operational need and no common runtime policy or Traccia
  telemetry boundary is required.
- The organization cannot own deployment, migrations, policies, upgrades, and incident response for a
  self-hosted runtime.
- The primary need is model training, a vector database, or an evaluation product. Algen Agent Runtime integrates
  these; it does not replace them.

## Why not call it another agent framework?

The useful boundary is narrower: normalized contracts and an enterprise execution kernel. Mature
external frameworks remain valid choices for specialized graphs and multi-agent collaboration. The
adapter SDK gives them a common result and event vocabulary; the native runtime serves agents that
benefit from Algen Agent Runtime's stricter state machine.

This preserves the operational differentiators: control, portability, reproducibility, auditability,
and observability.

## Adoption checklist

Adopt the framework when at least three are true:

- Multiple agents or products will share the runtime.
- Multiple model providers must be supported.
- Tool calls require tenant-aware authorization or human approval.
- Runs must be reconstructable from versioned configuration, state, and audit data.
- A central team needs uniform OpenTelemetry or Traccia signals.
- Implementations span multiple orchestration frameworks.
- Retrieval needs tenant isolation and replaceable vector backends.
- Fallback, cancellation, bounded retries, and budgets are production requirements.

Start with one bounded workload. Measure time to production, trace coverage, policy violations,
recovery behavior, latency overhead, and provider-switch effort. Expand only when the shared runtime
demonstrably reduces delivery or operating cost.

## Current maturity

This repository is a production-oriented foundation, not a claim that deployment is automatically
production-ready. Production users must select durable stores, configure authentication and secrets,
validate tenant isolation, load-test concurrency, rehearse recovery, and run optional integration tests
against their actual providers, frameworks, databases, and telemetry destination.
