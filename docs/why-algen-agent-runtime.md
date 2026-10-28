# Who should use Algen Agent Runtime—and why

Owner: AlgenAI Maintainers  
Status: Informational  
Last reviewed: 2026-10-28 (Runtime `0.1.0a15`)  

## Short answer

Use Algen Agent Runtime when an organization needs the same security, audit, approval, routing,
metering and observability rules across several agents, tenants, model providers, tools, or
orchestration frameworks. Do not adopt it merely to get a chatbot or basic tool-calling loop
running: every framework below does that well.

Algen Agent Runtime is a provider-neutral execution kernel for governed, multi-tenant agents. Its
native runtime puts tenant and caller identity, policy checks, tool side-effect controls, approvals,
budgets, metering and audit events inside one execution state machine, while domain behavior stays
in configuration, tools, retrieval data, workflows, and plugins.

## Good fits

### Enterprise AI platform teams

Platform teams supporting agents owned by many product groups need common run identifiers, tenant
isolation, model and tool allowlists, budgets, audit events, approval semantics, redaction, and
telemetry. Algen Agent Runtime centralizes those contracts while domain behavior remains in
configuration, tools, retrieval data, workflows, and plugins.

### Multi-tenant SaaS hosts

One runtime process can serve many customer organizations. Tenant and user context are part of
every run, store, approval and audit record; a `UsageMeter` is told which tenant, user, run and model
each model call belongs to (`MeterCall`), so the host charges the right account; and tools can act as
the signed-in user through per-run caller credentials that are kept in memory only and re-supplied
on every resume. Remote MCP servers that need OAuth (Slack, Linear, Jira) are connected per tenant
or per user by the Runtime.

### Teams operating multiple model providers

Use it when agents must move between OpenAI, Azure OpenAI, Anthropic, Mistral, DeepSeek, Ollama, or
Hugging Face without rewriting orchestration. Capability discovery prevents agents from silently
requesting tools, JSON Schema, images, or streaming from a model that cannot provide them, and
fallback stays inside the same policy and budget envelope.

### Regulated or side-effecting workflows

Agents changing records, executing operational actions, or accessing sensitive sources need more than
prompt instructions. Algen Agent Runtime classifies tools as read-only, write, externally visible,
or destructive; checks arguments against the tool's schema and an optional `precheck` before anyone
is asked to approve; applies authorization and policy checks; creates redacted approvals that can be
approved, modified or rejected; records each side effect in an idempotent tool-execution ledger; and
writes audit events.

### Traccia integrations

Algen Agent Runtime provides a consistent execution and telemetry vocabulary for Traccia-managed
solutions and acts as the reference implementation for Traccia instrumentation. External LangGraph,
OpenAI Agents SDK, AutoGen, and CrewAI graphs/agents can be registered via the trusted
Application Registration pattern (`build_container(framework_adapters=(...))`), wrapping the outer
execution in standard tenant, timeout, and trace envelopes (see [Framework adapters](framework-adapters.md)).
LangGraph and the OpenAI Agents SDK are supported adapters; AutoGen and CrewAI are experimental, and
AutoGen is archived upstream in favour of Microsoft Agent Framework, which has no adapter yet.

### Reusable agent products

Teams delivering substantially different agents can reuse the runtime while keeping domain logic out
of the core. Versioned definitions, declared plugins, deterministic providers, typed tools, and
pluggable storage reduce the operational code copied into every product.

## Poor fits

Algen Agent Runtime is probably unnecessary when:

- One small application uses one model, no sensitive data, and one or two read-only tools.
- A managed agent platform is sufficient and provider portability is unimportant.
- An existing framework already meets every operational need and no common runtime policy or Traccia
  telemetry boundary is required.
- The primary requirement is hardened multi-worker durable execution today; Temporal, or LangGraph
  with LangSmith Deployment, is the safer baseline (see [Current maturity](#current-maturity)).
- The application is TypeScript-first (Mastra is the TypeScript-native option) or needs the largest
  ready-made integration catalog.
- The organization cannot own deployment, migrations, policies, upgrades, and incident response for a
  self-hosted runtime.
- The primary need is model training, a vector database, or an evaluation product. Algen Agent Runtime integrates
  these; it does not replace them.

## How it compares

Snapshot: 2026-10-28, against Algen Agent Runtime `0.1.0a15`. Competitor rows summarize each
vendor's own documentation and release notes (see [Sources](#sources)); they are not independent
benchmarks, and absence from a vendor's docs is not proof that a feature is missing. These projects
change monthly: refresh this section at least quarterly and before using it in external material.

### Where each product sits

| Product | Latest (2026-10) | Category | Relationship to Algen |
| --- | --- | --- | --- |
| LangGraph + LangSmith Deployment | `langgraph` 1.2.18, GA | Stateful graph orchestration; managed or self-hosted Agent Server | Strongest alternative for durable graph workflows; can run behind an Algen adapter |
| OpenAI Agents SDK | 0.24.0, pre-1.0 | Lightweight agent SDK with handoffs, guardrails, sessions, MCP | SDK alternative; supported Algen adapter |
| Pydantic AI (+ Harness, alpha) | 2.63.0, GA | Typed, provider-neutral Python agents; durability through Temporal, DBOS, Prefect and others | Closest Python-first alternative; its alpha Harness overlaps Algen's governance pitch |
| Microsoft Agent Framework | Python 1.23.0, GA since 2026-04 | Successor to AutoGen and Semantic Kernel; agents, graph workflows, middleware | Direct alternative; AutoGen itself is archived and in maintenance mode |
| Google ADK | 2.13.0, GA | Agents and workflows with Google Cloud Agent Runtime as the managed path | Direct alternative, strongest on Google Cloud |
| CrewAI (+ AMP) | 1.15.32 | Multi-agent crews and flows; commercial management platform | Multi-agent alternative; experimental Algen adapter |
| Temporal | Python SDK 1.34.0 | Durable workflow engine with agent-framework integrations | Durability substrate rather than an agent framework; complements Algen |
| Strands Agents | 1.62.0, GA | Model-driven agents with interventions and Cedar tool authorization | Alternative for AWS-centred teams |
| Mastra | `@mastra/core` 1.80.0 | TypeScript agents, workflows and server | Alternative for TypeScript stacks |
| Claude Agent SDK | 0.2.174, alpha | Claude Code's agent loop as a library; Claude models only | Single-vendor contrast; not provider-neutral |
| NeMo Guardrails, Guardrails AI | 0.24.1, 0.11.0 | Guardrail layers | Complements to Runtime policy, not runtimes |

### Capability matrix

**Y** documented and built in · **P** partial, opt-in, experimental or left to the application ·
**N** not documented. Algen's column is a repository fact for `0.1.0a15`; the qualification
column says where it is still incomplete.

| Capability | Algen `0.1.0a15` | LangGraph | OpenAI Agents SDK | Pydantic AI | MS Agent Framework | Google ADK | CrewAI | Algen qualification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Provider neutrality and cross-provider fallback | Y | Y | P | Y | P | Y | P | Breadth of adapters trails LangChain and Pydantic AI |
| Typed tools and structured output | Y | Y | Y | Y | Y | Y | Y | — |
| Side-effect classes, idempotency keys, durable tool ledger | Y | P | P | P (alpha) | P | P | N | Operator reconciliation of indeterminate calls is not yet exposed |
| Durable approvals with approve, modify, reject | Y | Y | P | P | P | P | P | Restart and failover drills against live stores are still pending |
| Policy before and after tool calls; prechecks | Y | P | Y | Y (alpha) | Y (exp.) | Y | Y | Pattern-based; signed policy bundles are not yet available |
| Tenant and user in core contracts and stores | Y | P | N | P (alpha) | P | P | P | Per-tenant storage quotas are not yet available |
| Per-run caller credentials, never persisted | Y | P | P | P | Y | Y | P | Workflow tool steps and the HTTP API pass none in `0.1.0a15` |
| Budgets and usage metering per tenant and model | Y | P | P | Y (alpha) | P | P | P | Metering is a host-provided `UsageMeter`; the Runtime ships no ledger |
| Durable execution and crash recovery | P | Y | P | P | Y | P | P | Single-node checkpoint and `recover()`; multi-worker hardening pending |
| MCP: stdio, Streamable HTTP, SSE, OAuth | Y | Y (beta) | P | Y | P | Y | P | No dynamic client registration; stdio servers are unsandboxed |
| Multi-agent and workflow orchestration | Y | Y | P | Y | Y | Y | Y | Typed DAG manifests; graph-authoring ergonomics trail LangGraph |
| Context management (compaction, offloading, trimming) | Y | Y | P | Y | Y | Y | P | Token estimates are provider-neutral, not tokenizer-exact |
| Observability (OpenTelemetry and native) | Y | Y | P | Y | Y | Y | Y | — |
| Untrusted-document and prompt-injection defences | Y | N | P | P (alpha) | Y (exp.) | P | N | Heuristic; quarantine means human review, not a verdict |
| Repeat guards, per-tool call limits, wrap-up near budget | Y | P | P | Y (alpha) | P | P | P | — |
| Deployment and license | Library and self-hosted service, Apache-2.0 | Library MIT; paid managed or self-hosted server | Library MIT | Library MIT; managed add-ons | Library MIT; Foundry hosting | Library Apache-2.0; Google Cloud | Library MIT; commercial AMP | No managed service |

Temporal is left out of the matrix because it is a durability substrate, not an agent framework:
it is strongest on durable execution and weakest on agent-level governance, and it pairs well with
any of the others.

### Where Algen is differentiated

Use these claims only with their qualification:

1. **Tenant-aware core contracts.** Tenant, user, permissions, side effects, idempotency and caller
   credentials are part of the Runtime's contracts and stores, not application conventions. Every
   compared framework leaves tenancy to the application or the infrastructure, or offers it only in
   an alpha or experimental add-on. Qualification: deployment authentication and isolation tests
   remain the operator's responsibility.
2. **An idempotent side-effect ledger coupled to approvals.** Writes are keyed and single-attempt,
   and an approved call executes once. Most compared frameworks document at-least-once re-execution
   on resume or leave idempotency to the developer; only Pydantic AI's alpha Harness documents a
   ledger. Qualification: reconciliation APIs for indeterminate calls are still pending.
3. **Governance and accounting in one state machine.** The schema check, prechecks, policy
   decisions, approvals, repeat guards, budgets, metering and audit records are wired into the same
   run lifecycle and checkpointed with it, so they survive an approval pause and a restart. Competitors
   now offer many of these individually; few combine them, and several are alpha or experimental.
4. **Provider-neutral and multi-tenant at once.** Several competitors are provider-neutral, and a few
   have tenant features, but Algen's routing, fallback, capability narrowing, metering and MCP OAuth
   all carry the same tenant and run identity.

### Where competitors are ahead

- **Durable distributed execution.** Temporal, LangGraph with LangSmith Deployment, and Microsoft
  Agent Framework's durable tasks are production-proven across workers. Algen is hardened for single
  node; multi-worker claims, fencing and leader election are open work.
- **Ecosystem and authoring.** LangChain, Pydantic AI and CrewAI have larger integration catalogs,
  communities and tutorials; LangGraph has richer graph-authoring ergonomics.
- **Managed hosting.** LangSmith Deployment, Google Cloud Agent Runtime, Microsoft Foundry and
  CrewAI AMP offer managed platforms. Algen is self-hosted only.
- **Maturity signals.** LangGraph, Pydantic AI, Microsoft Agent Framework, Google ADK and Strands are
  GA; Algen is a `0.1.0` pre-alpha with experimental public APIs.
- **Language reach.** Microsoft Agent Framework (.NET, Python, and Go in preview) and Mastra
  (TypeScript) reach beyond Python; Algen is Python only.

### Claims to avoid

- "The only runtime that enforces policy in the execution path" or "fails closed": Microsoft Agent
  Framework's Agent Hooks, Strands' Cedar authorization and NeMo's tool rails also document this.
- "Competitors lack durable execution or human approval": most now document both.
- "The only framework with per-caller credentials": Microsoft Agent Framework and Google ADK document
  them too.
- "Production-ready" or "enterprise-ready" without the [production gate](production-readiness.md).
- "Uniform governance across LangGraph, CrewAI, AutoGen and OpenAI Agents": adapters govern the outer
  run; internal model and tool calls stay framework-owned.
- "Sandboxed MCP": stdio MCP servers run as trusted, unsandboxed host processes.

## Why not call it another agent framework?

The useful boundary is narrower: normalized contracts and an enterprise execution kernel. Mature
external frameworks remain valid choices for specialized graphs and multi-agent collaboration. The
adapter SDK gives them a common result and event vocabulary; the native runtime serves agents that
benefit from Algen Agent Runtime's stricter state machine.

This preserves the operational differentiators: control, portability, reproducibility, auditability,
and observability.

## Adoption checklist

Adopt the runtime when at least three are true:

- Multiple agents or products will share the runtime.
- One deployment serves several tenants, and model use must be charged or rationed per tenant.
- Multiple model providers must be supported.
- Tool calls require tenant-aware authorization, acting as the signed-in user, or human approval.
- Side effects must execute once, even across retries, approval pauses and restarts.
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

The main open gaps, tracked in [production readiness](production-readiness.md), are multi-worker
execution (distributed claims, fencing, leader election), operator reconciliation of indeterminate
tool calls, OIDC/JWKS and mTLS authentication options, sandboxing for stdio MCP servers and
subprocess tools, per-tenant storage quotas, and load, soak and failover evidence. Among adapters,
LangGraph and the OpenAI Agents SDK are supported; AutoGen and CrewAI remain experimental, and
AutoGen itself is now in maintenance mode upstream.

## Sources

Opened on 2026-10-28; versions from PyPI and npm on the same day.

- LangGraph: [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
  [functional API and durability](https://docs.langchain.com/oss/python/langgraph/functional-api),
  [Agent Server changelog](https://docs.langchain.com/langsmith/agent-server-changelog),
  [OpenTelemetry](https://docs.langchain.com/langsmith/trace-with-opentelemetry),
  [PyPI](https://pypi.org/project/langgraph/).
- OpenAI Agents SDK: [tracing](https://openai.github.io/openai-agents-python/tracing/),
  [sessions](https://openai.github.io/openai-agents-python/sessions/),
  [PyPI](https://pypi.org/project/openai-agents/).
- Pydantic AI: [documentation](https://ai.pydantic.dev/), [PyPI](https://pypi.org/project/pydantic-ai/),
  [Harness on PyPI](https://pypi.org/project/pydantic-ai-harness/).
- Microsoft Agent Framework: [overview](https://learn.microsoft.com/en-us/agent-framework/overview/),
  [AutoGen repository status](https://github.com/microsoft/autogen),
  [PyPI](https://pypi.org/project/agent-framework/).
- Google ADK: [resume](https://adk.dev/runtime/resume/), [traces](https://adk.dev/observability/traces/),
  [PyPI](https://pypi.org/project/google-adk/).
- CrewAI: [checkpointing](https://docs.crewai.com/v1.15.32/en/concepts/checkpointing.md),
  [tool hooks](https://docs.crewai.com/v1.15.32/en/learn/tool-hooks.md),
  [PyPI](https://pypi.org/project/crewai/).
- Temporal: [OpenAI Agents integration](https://docs.temporal.io/develop/python/integrations/openai-agents),
  [activities](https://docs.temporal.io/activity-definition),
  [PyPI](https://pypi.org/project/temporalio/).
- Strands Agents: [PyPI](https://pypi.org/project/strands-agents/). Mastra:
  [npm](https://www.npmjs.com/package/@mastra/core). Claude Agent SDK:
  [agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop.md),
  [PyPI](https://pypi.org/project/claude-agent-sdk/).
- NeMo Guardrails: [engine feature support](https://docs.nvidia.com/nemo/guardrails/latest/reference/engine-feature-support).
  Guardrails AI: [PyPI](https://pypi.org/project/guardrails-ai/).
