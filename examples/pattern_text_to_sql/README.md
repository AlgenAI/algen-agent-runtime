# OpenAI and Mistral Text-to-SQL Agent

This example turns an analytics question into SQLite SQL, executes it through a read-only Algen Agent Runtime tool, and asks either OpenAI or Mistral AI to summarize the verified rows. It is intentionally outside `src/algen_agent_runtime` so all schema and domain behavior remain example configuration and tooling.

## Run it

From the repository root:

```bash
export OPENAI_API_KEY='your-key-from-a-secret-manager'
python -m examples.text_to_sql_agent.app \
  "Show completed revenue by customer, highest first"
```

To use Mistral AI instead:

```bash
export MISTRAL_API_KEY='your-key-from-a-secret-manager'
python -m examples.text_to_sql_agent.app \
  --provider mistral \
  "Show completed revenue by customer, highest first"
```

The provider definitions, model profiles, and allowlists live in `agent.yaml`. Only the selected provider's API key is required. OpenAI remains the default, so existing commands continue to work.

The Mistral example uses `ministral-3b-2512`, which supports function calling. When changing models, update the provider's `default_model`, the agent's `default_model.model`, and `model_allowlist` together; the agent model profile takes precedence during routing.

The example schema documents its valid order statuses (`completed`, `pending`, and `cancelled`) in the system instructions. Consequently, phrases such as “completed orders” map directly to `orders.status = 'completed'` and do not trigger clarification.

If Mistral returns an HTTP error, Algen Agent Runtime includes its bounded, redacted error message and code. A `403 Forbidden` is a provider-side authorization or guardrail decision; verify that Studio is active, the API key belongs to the intended workspace, and that workspace can use the configured model.

When Traccia is enabled, the CLI closes the Algen Agent Runtime container before exiting so its batched spans are flushed to the ingestion endpoint. For exporter diagnostics, set `TRACCIA_DEBUG=true`; do not enable prompt/content capture when troubleshooting production data unless policy permits it.

Prompt, completion, and tool payload attributes are disabled by default. To include bounded, redacted `llm.prompt`, `llm.completion`, `tool.input`, and `tool.output` fields for this example, set `telemetry.include_content: true` in `agent.yaml`. Adjust `telemetry.max_content_chars` if the default 16,384-character limit is unsuitable.

The default question is used when no argument is supplied:

```bash
python -m examples.text_to_sql_agent.app
```

## Safety properties

- The model receives schema metadata but no database credentials.
- Only the `analytics.query` tool owns SQL execution.
- Only one `SELECT` or `WITH` statement is accepted.
- SQLite authorizer checks deny writes, DDL, transactions, attachments, and pragmas.
- Reads are restricted to `customers` and `orders`.
- Results are limited to 200 rows and 256 KiB.
- Tool calls are schema-validated, tenant-aware, idempotent, rate-bounded, traced, and audited by Algen Agent Runtime.

The SQLite database is seeded from `schema.sql` and exists only in memory. Replace `ReadOnlyAnalyticsDatabase` with a deployment-owned database adapter for production, retaining the same tool contract and enforcing database-level read-only credentials, statement timeouts, tenant filters, and row-level security.
