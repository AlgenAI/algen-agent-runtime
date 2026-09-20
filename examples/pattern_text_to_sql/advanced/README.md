# Text-to-SQL — Advanced Mode (pgvector schema retrieval)

This is the advanced mode of [pattern_text_to_sql](../README.md). It keeps schema objects,
metric definitions, known values, SQL constraints, and a few-shot query pattern in pgvector.
At run time, Algen Agent Runtime retrieves only the metadata relevant to the question, adds source IDs,
and gives it to an OpenAI-backed Text-to-SQL agent. The analytics data remains in a strictly
read-only SQLite fixture; pgvector is the durable knowledge catalog.

## Run it

Start the included pgvector service and install the optional backend:

```bash
docker compose up -d postgres
pip install -e '.[dev,pgvector]'
export OPENAI_API_KEY='...'
export PGVECTOR_DSN='postgresql://algen:local-only@localhost:5432/algen_agent_runtime'
```

The password above belongs only to the disposable local Compose database. Never commit deployment
credentials; provide `PGVECTOR_DSN` through a secret manager in production.

Create the collection and index the schema knowledge:

```bash
python -m examples.pattern_text_to_sql.advanced.app --index
```

Ask a question:

```bash
python -m examples.pattern_text_to_sql.advanced.app \
  "Show completed revenue by customer, highest first"
```

Re-run `--index` after schema or semantic metadata changes. Ingestion replaces each logical document
transactionally for the tenant, so retries do not create duplicate chunks.

For production, apply `migrations/001_schema_embeddings.sql` with a privileged migration identity,
set `initialize_schema: false`, and give the runtime identity only `SELECT`, `INSERT`, `UPDATE`, and
`DELETE` on this table. The migration is fixed to the 1,536 dimensions returned by
`text-embedding-3-small`; change both together when selecting another embedding model.
