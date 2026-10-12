-- Run requests can contain order-sensitive JSON Schema objects. PostgreSQL jsonb
-- canonicalizes object keys, so keep the serialized run state as order-preserving
-- json while retaining the separately indexed columns used for run queries.
ALTER TABLE algen_agent_runtime_runs
    ALTER COLUMN state TYPE json USING state::json;
