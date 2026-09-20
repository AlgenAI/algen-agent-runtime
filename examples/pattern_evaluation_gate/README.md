# pattern_evaluation_gate

**Tier:** Pattern | **Status:** 🚧 Placeholder — not yet implemented

> [!NOTE]
> This example will show how to evaluate an agent against golden fixtures, enforce acceptance thresholds, and make a promotion decision.

## Planned scope

- An agent with a corresponding evaluation fixture set (golden inputs and expected outputs).
- Evaluation runner that compares actual outputs against golden answers.
- Acceptance thresholds: percentage of cases that must pass before promotion.
- Regression output: a structured diff of passing/failing cases across runs.
- Promotion decision: pass/fail gate suitable for CI integration.

## Implementation checklist

- [ ] Create `agent.yaml`.
- [ ] Create `config/evaluation.yaml` with fixture paths, metrics, and thresholds.
- [ ] Create `data/` with golden input/output fixture files.
- [ ] Create `app.py` as the evaluation runner entry point.
- [ ] Create `manifest.yaml`.
- [ ] Create `tests/test_evaluation.py` that runs the gate deterministically.
- [ ] Update this README with setup/run/test/reset instructions and expected output.
