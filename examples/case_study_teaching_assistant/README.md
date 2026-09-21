# Virtual Teaching Assistant — Responsible AI demonstration

This example is a grounded, conversational teaching assistant designed to demonstrate how Traccia
Runtime and Traccia telemetry can make responsible-AI controls visible, enforceable, and auditable.
It is a collaboration concept for CeRAI, not a CeRAI-endorsed product.
See [`DEMO_GUIDE.md`](DEMO_GUIDE.md) for the presentation sequence, expected trace evidence, proposed
collaboration, and measurable success criteria.

## What the demo proves

| Theme | Agent behaviour | Evidence in Traccia Platform |
|---|---|---|
| Making AI understandable | Grounded answers, sources, and deterministic teaching tools | End-to-end retrieval, tool, model, latency, usage, and cost spans |
| AI & Safety | PII, prompt-injection, and output guardrails plus platform-managed cost policy | Explicit guardrail findings and `govern()` policy enforcement |
| AI & Society | Evidence cards plus academic-integrity and educational-equity guardrails | Tier-A findings show whether each safeguard ran and triggered |

The learner UI is intentionally a normal teaching product. It does not expose traces, policies,
guardrails, token usage, or runtime internals. Those belong in Traccia Platform during the demo.

Traccia Platform policy enforcement and Runtime guardrails are separate:

- The Traccia SDK `govern()` wrapper checks platform policy status before the agent invocation. The
  Runtime does not implement the Traccia cost policy.
- Runtime guardrails perform application checks. When Traccia is enabled, Runtime opens every check
  through the SDK's `guardrail_span()` helper and records name, category, triggered state, enforcement
  mode, boundary, and reason code. The SDK aggregates findings onto the conversation root trace for
  Guardrail Posture; detection itself does not enforce them.
- Runtime token, step, timeout, and local cost ceilings remain deterministic termination controls—not
  Traccia Platform policies.

## Run locally

```bash
pip install -e '.[dev,traccia]'
export OPENAI_API_KEY='...'
python -m examples.case_study_teaching_assistant.dashboard
```

Open <http://localhost:8091>. Local mode uses in-memory persistence and development identity headers.
To send traces to Traccia:

```bash
export TRACCIA_API_KEY='...'
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__ENABLED=true
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__GOVERNANCE_ENABLED=true
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__GOVERNANCE_FAIL_OPEN=false
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__GOVERNANCE_AGENT_ID=virtual-teaching-assistant
python -m examples.case_study_teaching_assistant.dashboard
```

Create and activate the desired spend policy in Traccia Platform for the same agent ID before the
demo. `govern()` applies Warn/Block state at the start of the next invocation; it does not interrupt a
request that already completed.

The CLI uses the same agent and tool contracts:

```bash
python -m examples.case_study_teaching_assistant.app \
  'I missed probability class and have 7 days with 60 minutes daily to catch up. Make a plan.'
```

## Suggested live demo sequence

1. Select “Catch up after missing class.” The fictional student introduces herself and includes a
   synthetic student email incidentally while asking for a useful study plan. Inspect the `pii`
   finding and `teaching.build_study_plan` tool span in Traccia Platform; the assistant should still
   complete the learning task without echoing the identifier.
2. Select the class-debate or research-claim scenario. Inspect the deterministic evidence tool and
   the guardrails that ran around both model and tool boundaries.
3. Request a final answer to graded work and show the tutor-mode intervention.
4. Optionally try a direct system-prompt extraction instruction to demonstrate the
   `prompt_injection` block.
5. Demonstrate the platform cost policy through the SDK `govern()` boundary.
6. Compare the unchanged learner experience with trace, Guardrail Posture, policy, and audit evidence.

## Structure

```text
case_study_teaching_assistant/
├── application/          # Handler, education guardrails, and deterministic teaching tools
├── config/agent.yaml     # Local agent, RAG, safety, budget, and telemetry configuration
├── config/ecs.yaml       # Production persistence, JWT, Redis cache, and Traccia overlay
├── deploy/               # ECS task template and operational notes
├── ui/index.html         # Dependency-free, same-origin conversational demo
├── app.py                # Library/CLI path
├── dashboard.py          # FastAPI composition root
└── Dockerfile
```

Generic PII redaction, secret protection, bounded execution, audit logging, tenant isolation,
telemetry, persistence, and conversation transport live in `src/algen_agent_runtime`. Only tutor behaviour
and education-specific policy plugins live in this example.

## Important boundaries and next collaboration steps

- This assistant must not grade, proctor, diagnose, or make admissions/disciplinary decisions.
- Regex PII detection is a baseline control, not a complete institutional DLP system. A real pilot
  should add institution-specific identifiers, multilingual evaluation, consent, deletion workflows,
  and red-team tests.
- Before curriculum use, co-design learning outcomes, rubrics for trace literacy, accessibility,
  policy case studies, and an evaluation set with faculty and learners.
- Measure learning benefit, hallucination/citation quality, subgroup experience, false-positive policy
  interventions, privacy leakage, latency, and cost—not just answer accuracy.
- Use synthetic learner identities in a demo. Production learner records require a data-processing
  agreement, retention schedule, role-based access, and an incident-response process.

For AWS, see [`deploy/README.md`](deploy/README.md). Multi-task ECS deployment must use the supplied
PostgreSQL/Redis overlay; in-memory state is only suitable for a single-process demonstration.
The production UI accepts an OAuth/OIDC access token from the one-time URL fragment
`#access_token=...`, moves it to session storage, and removes it from the address bar. A real portal
should use Authorization Code + PKCE rather than constructing this fragment manually.
