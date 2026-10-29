# Cloud chat quickstart

The smallest hosted-provider example: one agent, an explicit model allowlist, guardrails, verification,
and budgets. Set `OPENAI_API_KEY`, then run:

```bash
python -m examples.quickstart_cloud_chat.app "Explain governed agents in one sentence."
```

Without the key the CLI stops with a hint. To try it without a key or network, add `--offline`: the
same `agent.yaml` runs with its `openai` provider swapped for the deterministic mock provider (see
`examples/offline.py`), which returns a scripted answer. Guardrails, verification and budgets still
run:

```bash
python -m examples.quickstart_cloud_chat.app --offline
```

Import `agent.yaml` into Studio to inspect and run `cloud-chat`. Without `--offline` this example
makes a paid network request. It uses in-memory stores and is not a deployment template.
