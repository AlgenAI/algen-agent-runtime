# Cloud chat quickstart

The smallest hosted-provider example: one agent, an explicit model allowlist, guardrails, verification,
and budgets. Set `OPENAI_API_KEY`, then run:

```bash
python -m examples.quickstart_cloud_chat.app "Explain governed agents in one sentence."
```

Import `agent.yaml` into Studio to inspect and run `cloud-chat`. This example makes a paid network
request. It uses in-memory stores and is not a deployment template.
