# CeRAI Responsible AI demonstration guide

## Demonstration thesis

Responsible AI becomes teachable when participants can use a realistic agent, then inspect independent
evidence of how it behaved. The learner sees only the teaching assistant. The presenter uses Traccia
Platform separately to inspect traces, Guardrail Posture, policy enforcement, and audit evidence.

## Fifteen-minute flow

| Minute | Action | Expected visible result | Traccia evidence |
|---:|---|---|---|
| 0–2 | Introduce the teaching assistant | A clean, realistic learner experience | conversation and agent root spans |
| 2–5 | Select the catch-up scenario, where a fictional student includes an email while asking for a seven-day plan | Useful schedule without echoing the identifier | `pii` warning, `teaching.build_study_plan`, model, verification, usage, and latency |
| 5–8 | Prepare for the facial-recognition class debate or evaluate the AI-tutor research claim | Evidence-led safety and society analysis | `teaching.get_case_evidence` plus explicit guardrails around tool and model boundaries |
| 8–10 | Ask for a submission-ready graded assignment answer | Tutor guidance instead of completed assessed work | `academic_integrity.tutor_mode` and policy lookup tool |
| 10–12 | Try a direct prompt-extraction attempt | Bounded refusal | `prompt_injection` block finding |
| 12–15 | Activate/test a platform cost policy | The next governed invocation follows platform status | SDK `govern()` check, policy violation, cost evidence |

Use only synthetic identities during the demonstration. Keep telemetry content capture disabled. Show
metadata, policy reason codes, findings, and source identifiers in Traccia Platform—not in the agent UI.

## Collaboration proposal

Ask CeRAI to contribute real educational-agent use cases, define responsible behaviour and failure
rubrics, and independently evaluate the traces and policy interventions. A useful joint pilot would:

1. create a multilingual, Indian-context evaluation corpus;
2. compare trace-assisted teaching with answer-only teaching for learners and practitioners;
3. measure privacy leakage, citation grounding, unsafe-compliance, fairness, accessibility, cost, and
   false-positive guardrail interventions;
4. publish a trace-literacy lab and auditable assessment rubric suitable for curricula;
5. document limits: an execution trace is governance evidence, not proof that a model is safe or a
   faithful window into its hidden reasoning.

## Success criteria

- Every response maps to a run and conversation trace.
- Platform policy enforcement occurs through SDK `govern()`, not a Runtime policy implementation.
- Every Runtime guardrail emits name, category, triggered state, and enforcement mode.
- Traccia SDK aggregation writes the detected guardrail summary onto the root conversation trace.
- Questions that require planning, case evidence, or policy lookup produce typed Runtime tool spans.
- Direct identifiers do not appear in model-facing payloads or exported telemetry.
- An active platform Block state fails closed at the next governed invocation.
- Retrieved factual claims contain source attribution.
- Academic-integrity interventions remain helpful instead of only refusing.
- Equity interventions do not replace evidence with generic moralising.
- Faculty can replay a scenario from versioned agent configuration, inputs, source set, and metadata.
