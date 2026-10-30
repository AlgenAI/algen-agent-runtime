# OpenAI and Mistral Text-to-SQL Agent

This example turns an analytics question into SQLite SQL, executes it through a read-only Algen Agent Runtime tool, and asks either OpenAI or Mistral AI to summarize the verified rows. It is intentionally outside `src/algen_agent_runtime` so all schema and domain behavior remain example configuration and tooling.

## Run it

From the repository root:

```bash
export OPENAI_API_KEY='your-key-from-a-secret-manager'
python -m examples.pattern_text_to_sql.app \
  "Show completed revenue by customer, highest first"
```

To use Mistral AI instead:

```bash
export MISTRAL_API_KEY='your-key-from-a-secret-manager'
python -m examples.pattern_text_to_sql.app \
  --provider mistral \
  "Show completed revenue by customer, highest first"
```

The provider definitions, model profiles, and allowlists live in `agent.yaml`. Only the selected provider's API key is required; without it the CLI stops with a hint. OpenAI remains the default, so existing commands continue to work.

To try it without a key or network, add `--offline`. The same `agent.yaml` runs with both providers swapped for the deterministic mock provider (see `examples/offline.py`). The scripted model always asks for completed revenue by region, whatever the question; the query still goes through `analytics.query`, its schema check, the SQLite authorizer and the row limit:

```bash
python -m examples.pattern_text_to_sql.app --offline
```

The Mistral example uses `ministral-3b-2512`, which supports function calling. When changing models, update the provider's `default_model`, the agent's `default_model.model`, and `model_allowlist` together; the agent model profile takes precedence during routing.

The example schema documents its valid order statuses (`completed`, `pending`, and `cancelled`) in the system instructions. Consequently, phrases such as “completed orders” map directly to `orders.status = 'completed'` and do not trigger clarification.

If Mistral returns an HTTP error, Algen Agent Runtime includes its bounded, redacted error message and code. A `403 Forbidden` is a provider-side authorization or guardrail decision; verify that Studio is active, the API key belongs to the intended workspace, and that workspace can use the configured model.

Traccia export is off in `agent.yaml` (`telemetry.traccia.enabled: false`), so the example runs without the `traccia` extra. To turn it on, install the extra and enable it through the environment:

```bash
pip install -e '.[dev,traccia]'
export TRACCIA_API_KEY='...'
export ALGEN_AGENT_RUNTIME__TELEMETRY__TRACCIA__ENABLED=true
```

When Traccia is enabled, the CLI closes the Algen Agent Runtime container before exiting so its batched spans are flushed to the ingestion endpoint. For exporter diagnostics, set `TRACCIA_DEBUG=true`; do not enable prompt/content capture when troubleshooting production data unless policy permits it.

Prompt, completion, and tool payload attributes are disabled by default. To include bounded, redacted `llm.prompt`, `llm.completion`, `tool.input`, and `tool.output` fields for this example, set `telemetry.include_content: true` in `agent.yaml`. Adjust `telemetry.max_content_chars` if the default 16,384-character limit is unsuitable.

The default question is used when no argument is supplied:

```bash
python -m examples.pattern_text_to_sql.app
```

## Safety properties

- The model receives schema metadata but no database credentials.
- Only the `analytics.query` tool owns SQL execution.
- Only one `SELECT` or `WITH` statement is accepted.
- SQLite authorizer checks deny writes, DDL, transactions, attachments, and pragmas.
- Reads are restricted to `customers` and `orders`.
- Results are limited to 200 rows and 256 KiB.
- Tool calls are schema-validated, tenant-aware, idempotent, rate-bounded, traced, and audited by Algen Agent Runtime.

`analytics.query` is registered by `AnalyticsPlugin` in `plugin.py`, which `agent.yaml` declares in
`domain_plugins` (trusted through `security.trusted_plugin_prefixes`), so
`algen-agent-runtime describe` and Studio can see the tool. The plugin hands the database to the
container, which closes it. Check the project with
`algen-agent-runtime check examples/pattern_text_to_sql/agent.yaml --path . --project-root examples/pattern_text_to_sql`
from the repository root.

The SQLite database is seeded from `schema.sql`, exists only in memory, and connects on first query. Replace `ReadOnlyAnalyticsDatabase` with a deployment-owned database adapter for production, retaining the same tool contract and enforcing database-level read-only credentials, statement timeouts, tenant filters, and row-level security.
