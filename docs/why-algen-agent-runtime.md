# Who should use Algen Agent Runtime—and why

Owner: AlgenAI Maintainers  
Status: Informational  
Last reviewed: 2026-11-16 (Runtime `0.2.0b5`; competitor versions and the cells listed under
[How it compares](#how-it-compares) checked the same day)  

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

Checked on 2026-11-16 against Runtime `0.2.0b5` (everything released through `0.2.0b4`, verified
in the source). Versions and release dates come from PyPI and npm on that day. Competitor rows
summarize each vendor's own documentation and release notes (see [Sources](#sources)); they are
not independent benchmarks, and absence from a vendor's docs is not proof that a feature is
missing. These projects change monthly: refresh this section at least quarterly and before using
it in external material.

On 2026-11-16 the capability cells were re-checked for LangGraph with LangChain 1.x (persistence,
human-in-the-loop, middleware, MCP adapters, Agent Server authorization), the OpenAI Agents SDK
(approvals, MCP), Pydantic AI and its Harness, Microsoft Agent Framework, Google ADK (tool
confirmation) and CrewAI (human feedback in flows). The LangGraph re-check changed three cells
(policy, injection defences, repeat guards); the others changed only the notes below the matrix.
Strands and the Claude Agent SDK overviews were re-read for the product table; Mastra and the
guardrail layers carry the 2026-10-28 assessment (see [Not re-verified](#not-re-verified)).

### Where each product sits

| Product | Latest (2026-11-16) | Category | Relationship to Algen |
| --- | --- | --- | --- |
| LangGraph + LangSmith Deployment | `langgraph` 1.2.22 (2026-11-14), stable; `langchain` 1.5.0 | Stateful graph orchestration; managed or self-hosted Agent Server | Strongest alternative for durable graph workflows; can run behind a supported Algen adapter |
| OpenAI Agents SDK | `openai-agents` 0.24.0 (2026-10-15), pre-1.0 | Lightweight agent SDK with handoffs, guardrails, sessions, tool approvals, MCP | SDK alternative; supported Algen adapter |
| Pydantic AI (+ Harness, alpha) | `pydantic-ai` 2.68.0 (2026-11-12), stable; `pydantic-ai-harness` 0.68.0, alpha | Typed, provider-neutral Python agents; durability through Temporal, DBOS, Prefect and others | Closest Python-first alternative; its alpha Harness (guardrails, approvals, per-tenant spend limits) overlaps Algen's governance pitch |
| Microsoft Agent Framework | `agent-framework` 1.26.0 (2026-11-12), stable; Go in public preview | Successor to AutoGen and Semantic Kernel; agents, Harness Agent, graph and functional workflows, middleware | Direct alternative; AutoGen itself is archived and no longer maintained |
| Google ADK | `google-adk` 2.14.1 (2026-11-12) | Agents and graph workflows with Google Cloud as the managed path | Direct alternative, strongest on Google Cloud |
| CrewAI (+ AMP) | `crewai` 1.15.41 (2026-11-12) | Multi-agent crews and flows; commercial management platform | Multi-agent alternative; experimental Algen adapter |
| Temporal | `temporalio` 1.36.1 (2026-11-14) | Durable workflow engine with agent-framework integrations | Durability substrate rather than an agent framework; complements Algen |
| Strands Agents | `strands-agents` 1.65.0 (2026-11-13), stable | Model-driven agents with interventions and Cedar tool authorization | Alternative for AWS-centred teams |
| Mastra | `@mastra/core` 1.83.0 (2026-11-12) | TypeScript agents, workflows and server | Alternative for TypeScript stacks |
| Claude Agent SDK | `claude-agent-sdk` 0.2.182 (2026-11-13), alpha | Claude Code's agent loop as a library; Claude models only | Single-vendor contrast; not provider-neutral |
| NeMo Guardrails, Guardrails AI | 0.24.1 (2026-09-16, beta), 0.11.0 (2026-08-14) | Guardrail layers | Complements to Runtime policy, not runtimes |

AutoGen's README says the project is archived and no longer maintained, with no bug or security
fixes, and points new projects to Microsoft Agent Framework; the last `autogen-agentchat` release
is 0.7.5 (2025-09-30).

### Capability matrix

**Y** documented and built in · **P** partial, opt-in, experimental or left to the application ·
**N** not documented. Algen's column is a repository fact for `0.2.0b5`; the qualification column
says where it is still incomplete and which release added the newest part.

| Capability | Algen `0.2.0b5` | LangGraph + LangChain | OpenAI Agents SDK | Pydantic AI | MS Agent Framework | Google ADK | CrewAI | Algen qualification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Provider neutrality and cross-provider fallback | Y | Y | P | Y | P | Y | P | Breadth of adapters trails LangChain and Pydantic AI |
| Typed tools and structured output | Y | Y | Y | Y | Y | Y | Y | — |
| Side-effect classes, idempotency keys, durable tool ledger | Y | P | P | P (alpha) | P | P | N | Audited, tenant-scoped reconciliation of indeterminate calls; supported on a single node |
| Durable approvals with approve, modify, reject | Y | Y | P | P | P | P | P | Restart drills against PostgreSQL and Redis; decisions other than approve, reject or modify are refused (`0.2.0b4`); multi-worker failover is experimental |
| Policy before and after tool calls; prechecks | Y | Y | Y | Y (alpha) | Y (exp.) | Y | Y | Pattern-based rules; signed policy bundles; mandatory policies fail closed and streamed text passes output policies (`0.2.0b4`) |
| Tenant and user in core contracts and stores | Y | P | N | P (alpha) | P | P | P | Per-tenant quotas; artifacts owned by a user (`0.2.0b4`); isolation testing is the operator's |
| Per-run caller credentials, never persisted | Y | P | P | P | Y | Y | P | HTTP callers forward headers listed in `api.credential_headers` and a recovered run waits for credentials (`0.2.0b3`); workflow tool steps get none |
| Budgets and usage metering per tenant and model | Y | P | P | Y (alpha) | P | P | P | Metering is a host-provided `UsageMeter`; the Runtime ships no ledger. A child run counts against its parent's budget (`0.2.0b4`) |
| Durable execution and crash recovery | P | Y | P | P | Y | P | P | Single node: checkpoints, `recover()`, conversation turns that survive restarts and deploys (`0.2.0b3`); more than one process per database is experimental |
| MCP: stdio, Streamable HTTP, SSE, OAuth | Y | Y | P | Y | P | Y | P | No dynamic client registration; stdio servers are sandboxed only through an operator-supplied wrapper; OAuth callback bound to the starting browser (`0.2.0b4`) |
| Multi-agent and workflow orchestration | Y | Y | P | Y | Y | Y | Y | Typed DAG manifests; delegation cannot widen a run's tools, approvals or budget, `max_descendants` caps a tree (`0.2.0b4`); graph-authoring ergonomics trail LangGraph |
| Context management (compaction, offloading, trimming) | Y | Y | P | Y | Y | Y | P | Oversized tool results offloaded or cut (`0.2.0b3`); token estimates are provider-neutral, not tokenizer-exact |
| Observability (OpenTelemetry and native) | Y | Y | P | Y | Y | Y | Y | — |
| Untrusted-document and prompt-injection defences | Y | P | P | P (alpha) | Y (exp.) | P | N | Heuristic, with Unicode folding (`0.2.0b4`) and retrieved-chunk screening (`0.2.0b3`); quarantine means human review, not a verdict |
| Repeat guards, per-tool call limits, wrap-up near budget | Y | Y | P | Y (alpha) | P | P | P | `budget.on_limit: summarize` answers from what the run has (`0.2.0b3`) |
| Deployment and license | Library and self-hosted service, Apache-2.0 | Library MIT; paid managed or self-hosted server | Library MIT | Library MIT; managed add-ons | Library MIT; Foundry hosting | Library Apache-2.0; Google Cloud | Library MIT; commercial AMP | No managed service |

Notes from the 2026-11-16 check:

- **LangGraph + LangChain 1.x.** LangChain's `create_agent` runs on LangGraph and ships built-in
  middleware: human-in-the-loop (approve, edit, reject or respond per tool call, through LangGraph
  interrupts and a required checkpointer), PII detection (redact, mask, hash or block), model and
  tool call limits, model fallback, summarization and context editing, and a beta "auto mode" that
  uses a decision model to block risky tool calls. That moves the policy and call-limit cells to
  **Y** and injection defences to **P** (no dedicated prompt-injection screen is documented).
  Checkpointers include in-memory, SQLite and PostgreSQL. The persistence and durable-execution
  pages do not describe idempotent side effects, so the ledger cell stays **P**. MCP comes through
  `langchain-mcp-adapters` (stdio, Streamable HTTP, SSE; OAuth through the MCP SDK's `httpx.Auth`).
  LangSmith Agent Server supports custom authentication and per-owner filters on threads and runs;
  tenancy is built from those handlers, so it stays **P**.
- **OpenAI Agents SDK.** Tools and MCP servers can require approval (`needs_approval`,
  `require_approval`); a paused run's `RunState` is serialized and resumed by the application,
  which also stores and verifies it. The docs describe approve and reject, not modifying the
  arguments, so approvals stay **P**. MCP covers stdio, Streamable HTTP, SSE and hosted servers;
  OAuth is left to an `httpx.Auth` the application supplies, so MCP stays **P**.
- **Pydantic AI.** Durable execution through Temporal, DBOS and Prefect (first-party), deferred
  tools for human approval, MCP and OpenTelemetry are in core. The alpha Harness adds guardrails
  with secret masking, approval reviewers, and per-model and per-tenant spend limits; its PyPI page
  does not describe an idempotent tool ledger (see [Not re-verified](#not-re-verified)).
- **Microsoft Agent Framework.** The new Harness Agent bundles planning, context compaction,
  "don't ask again" tool approval and observability; workflows hold state for long-running and
  human-in-the-loop work.
- **Google ADK.** Tool Confirmation pauses a tool for a yes/no answer or structured input, and graph
  workflows have human-input nodes; a modified-arguments decision is not described.
- **CrewAI.** `@human_feedback` in flows pauses, persists the flow (SQLite by default) and resumes.

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
   on resume or leave idempotency to the developer (Pydantic AI's alpha Harness was reported to
   document one on 2026-10-28; not re-verified). A call interrupted mid-flight becomes `indeterminate` and is settled through an audited
   reconciliation API, the tool's own `reconcile` hook or its compensation. Qualification:
   supported on a single node.
3. **Governance and accounting in one state machine.** The schema check, prechecks, policy
   decisions, approvals, repeat guards, budgets, metering and audit records are wired into the same
   run lifecycle and checkpointed with it, so they survive an approval pause and a restart. Competitors
   now offer many of these individually; few combine them, and several are alpha or experimental.
4. **Provider-neutral and multi-tenant at once.** Several competitors are provider-neutral, and a few
   have tenant features, but Algen's routing, fallback, capability narrowing, metering and MCP OAuth
   all carry the same tenant and run identity, and delegated child runs inherit their parent's
   tools, approval rules, guardrails and budget instead of widening them.

### Where competitors are ahead

- **Durable distributed execution.** Temporal, LangGraph with LangSmith Deployment, and Microsoft
  Agent Framework's durable tasks are production-proven across workers. Algen `1.0` supports one
  Runtime process per database ([deployment scope](production-readiness.md#deployment-scope));
  more than one process and the distributed worker pool are experimental, and recovery leases for
  several replicas are planned for `0.3`.
- **Ecosystem and authoring.** LangChain, Pydantic AI and CrewAI have larger integration catalogs,
  communities and tutorials; LangGraph has richer graph-authoring ergonomics.
- **Managed hosting.** LangSmith Deployment, Google Cloud Agent Runtime, Microsoft Foundry and
  CrewAI AMP offer managed platforms. Algen is self-hosted only.
- **Maturity signals.** LangGraph, Pydantic AI, Microsoft Agent Framework and Strands mark their
  packages production/stable on PyPI, and Google ADK is on its second major version; Algen is a
  `0.2` beta (`0.2.0b5`, the last before `0.2.0`) whose public API is the `1.0` candidate, not yet
  GA.
- **Language reach.** Microsoft Agent Framework (.NET, Python, and Go in preview) and Mastra
  (TypeScript) reach beyond Python; Algen is Python only.

### Claims to avoid

- "The only runtime that enforces policy in the execution path" or "fails closed": Microsoft Agent
  Framework's Agent Hooks, LangChain's middleware, Strands' Cedar authorization and NeMo's tool
  rails also document this.
- "Competitors lack durable execution or human approval": most now document both.
- "The only framework with per-caller credentials": Microsoft Agent Framework and Google ADK document
  them too.
- "Production-ready" or "enterprise-ready" without the [production gate](production-readiness.md).
- "Horizontally scalable" or "multi-worker": the supported scope is one process per database.
- "Uniform governance across LangGraph, CrewAI, AutoGen and OpenAI Agents": adapters govern the outer
  run; internal model and tool calls stay framework-owned.
- "Sandboxed MCP": stdio MCP servers and subprocess tools are host processes. `security.sandbox`
  limits their environment and resources and can wrap them in an operator-supplied sandbox
  (`bwrap`, `nsjail`, `firejail`, `sandbox-exec`); the Runtime does not isolate them by itself.

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

`0.2.0b5` is the last beta before `0.2.0`; the `1.0` release criteria are in
[GA criteria](ga-criteria.md). The supported `1.0` scope is a single node: one Runtime process per
database ([deployment scope](production-readiness.md#deployment-scope)). Every P0 item in
[production readiness](production-readiness.md) is done for that scope: reconciliation of
indeterminate tool calls, OIDC authentication, signed policy bundles, sandbox options for stdio MCP
servers and subprocess tools, per-tenant quotas and resource bounds, and persistence drills against
PostgreSQL and Redis. Single-node performance, load and noisy-neighbour results are published in
[operations](operations.md#performance-and-capacity-evidence). Running more than one process per
database and the distributed worker pool are experimental. Among adapters, LangGraph and the OpenAI
Agents SDK are supported; AutoGen and CrewAI remain experimental, and AutoGen itself is archived
upstream, so its adapter is frozen.

## Sources

Opened on 2026-11-16 unless marked; versions and release dates from the PyPI JSON API
(`https://pypi.org/pypi/<package>/json`) and the npm registry on the same day.

- LangGraph (2026-10-28): [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts),
  [functional API and durability](https://docs.langchain.com/oss/python/langgraph/functional-api),
  [Agent Server changelog](https://docs.langchain.com/langsmith/agent-server-changelog),
  [OpenTelemetry](https://docs.langchain.com/langsmith/trace-with-opentelemetry);
  (2026-11-16) [MCP adapters](https://docs.langchain.com/oss/python/langchain/mcp),
  [PyPI](https://pypi.org/project/langgraph/).
- OpenAI Agents SDK: [human in the loop](https://openai.github.io/openai-agents-python/human_in_the_loop/),
  [MCP](https://openai.github.io/openai-agents-python/mcp/),
  [tracing](https://openai.github.io/openai-agents-python/tracing/) (2026-10-28),
  [sessions](https://openai.github.io/openai-agents-python/sessions/) (2026-10-28),
  [PyPI](https://pypi.org/project/openai-agents/).
- Pydantic AI: [overview](https://pydantic.dev/docs/ai/overview/) (formerly ai.pydantic.dev),
  [PyPI](https://pypi.org/project/pydantic-ai/),
  [Harness on PyPI](https://pypi.org/project/pydantic-ai-harness/).
- Microsoft Agent Framework: [overview](https://learn.microsoft.com/en-us/agent-framework/overview/),
  [AutoGen repository status](https://github.com/microsoft/autogen),
  [PyPI](https://pypi.org/project/agent-framework/).
- Google ADK: [tool confirmation](https://adk.dev/tools-custom/confirmation/),
  [resume](https://adk.dev/runtime/resume/) (2026-10-28),
  [traces](https://adk.dev/observability/traces/) (2026-10-28),
  [PyPI](https://pypi.org/project/google-adk/).
- CrewAI: [human feedback in flows](https://docs.crewai.com/en/learn/human-feedback-in-flows),
  [checkpointing](https://docs.crewai.com/v1.15.32/en/concepts/checkpointing.md) (2026-10-28),
  [tool hooks](https://docs.crewai.com/v1.15.32/en/learn/tool-hooks.md) (2026-10-28),
  [PyPI](https://pypi.org/project/crewai/).
- Temporal (2026-10-28): [OpenAI Agents integration](https://docs.temporal.io/develop/python/integrations/openai-agents),
  [activities](https://docs.temporal.io/activity-definition);
  [PyPI](https://pypi.org/project/temporalio/).
- LangChain 1.x: [human-in-the-loop middleware](https://docs.langchain.com/oss/python/langchain/human-in-the-loop),
  [built-in middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in),
  [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence),
  [durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution),
  [Agent Server authentication and access control](https://docs.langchain.com/langsmith/auth),
  [PyPI](https://pypi.org/project/langchain/).
- Strands Agents: [documentation](https://strandsagents.com/latest/),
  [PyPI](https://pypi.org/project/strands-agents/). Mastra:
  [npm](https://www.npmjs.com/package/@mastra/core). Claude Agent SDK:
  [overview](https://code.claude.com/docs/en/agent-sdk/overview),
  [agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop.md) (2026-10-28),
  [PyPI](https://pypi.org/project/claude-agent-sdk/).
- NeMo Guardrails: [engine feature support](https://docs.nvidia.com/nemo/guardrails/latest/reference/engine-feature-support)
  (2026-10-28), [PyPI](https://pypi.org/project/nemoguardrails/).
  Guardrails AI: [PyPI](https://pypi.org/project/guardrails-ai/).
- Algen: the source at `0.2.0b5` and the release notes in [CHANGELOG](../CHANGELOG.md); each
  "since" release in the matrix was checked against the git tags.

### Not re-verified

- Mastra, NeMo Guardrails and Guardrails AI: carried from 2026-10-28; only their versions were
  re-checked. Strands and the Claude Agent SDK are not in the matrix; their overview pages were
  re-read on 2026-11-16 (Strands: interrupts, Cedar authorization, session persistence, MCP,
  OpenTelemetry; Claude Agent SDK: permissions, hooks, sessions, subagents, MCP), but the claim
  that the Claude Agent SDK runs Claude models only was not re-checked.
- LangGraph's OpenTelemetry export and the Agent Server changelog: carried from 2026-10-28.
- Google ADK tool confirmation and CrewAI human feedback: confirmed from documentation search
  results, not a full read of the pages.
- Pydantic AI Harness idempotent tool ledger: claimed on 2026-10-28; its PyPI page on 2026-11-16
  lists step persistence (save, resume, fork) but no ledger.
- Microsoft Agent Framework's GA date: its PyPI classifier says production/stable; the date it
  became GA was not re-checked.
- Per-caller credentials in Microsoft Agent Framework and Google ADK, and the "(exp.)" status of
  Agent Framework policy and prompt-injection features: carried from 2026-10-28.
