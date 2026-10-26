# Agent tool quickstart

Two CRM tools written as typed functions, grouped in a `Toolkit` and declared through a plugin,
so Studio and `algen-agent-runtime describe` see them:

| Tool | Preset | What it means |
| --- | --- | --- |
| `crm.find_account` | `read()` | `crm.read` permission, idempotent, retried on transient errors |
| `crm.update_owner` | `write(approval=True)` | `crm.write`, keyed by the idempotency key, one attempt, a person approves every call |

```text
tools.py   the crm Toolkit, its shared client and both tools
plugin.py  CrmPlugin, which registers the toolkit
agent.yaml names the plugin in domain_plugins and trusts its package
app.py     an offline run: the model looks the account up, asks to change the owner, waits for approval
```

Run it from the repository root; no credentials or services are needed:

```bash
python -m examples.quickstart_agent_tool.app sam            # approve the change
python -m examples.quickstart_agent_tool.app sam --reject   # reject it
algen-agent-runtime describe --config examples/quickstart_agent_tool/agent.yaml --path .
algen-agent-runtime check examples/quickstart_agent_tool/agent.yaml --path . \
  --project-root examples/quickstart_agent_tool
```

`check` warns that `app.py` builds its own container: Studio runs the agent from the settings, not
this script. The mock model is scripted with `MockModelProvider.queue`; with a real provider, drop
the queue and the model chooses the calls.

To adapt it, replace `InMemoryCrm` with a real client opened in `open_crm`, resolve its credentials
there with `await secrets("env://CRM_API_TOKEN")`, and list `CRM_API_TOKEN` in the toolkit's `env`.
Test each tool with `assert_tool_contract` and `fake_tool_context` from
`algen_agent_runtime.tools.testing`. See `docs/tool-authoring.md`.
