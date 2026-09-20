# Evaluation promotion gate

Runs versioned golden questions through Runtime's `EvaluationRunner`, checking expected plan nodes,
required and forbidden SQL, numerical expectations, and allowed/forbidden claims. Promotion requires
every case to pass and the aggregate score to meet the threshold.

```bash
python -m examples.pattern_evaluation_gate.app
```

`agent.yaml` also exposes the evaluation gate as a Studio-importable workflow resource. The included
fixtures are deliberately deterministic; replace them with domain-owned golden cases in production.
