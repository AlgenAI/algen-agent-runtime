-- Vectors for semantic and hybrid learned-memory recall (learned_memory_policy.recall).
--
-- The embedding is a real[] rather than a pgvector column so that this migration, which every
-- deployment runs, needs no extension: CREATE EXTENSION usually requires a superuser, and a
-- migration that depended on it would fail or leave the schema history different between
-- databases with and without pgvector. Recall ranks within one tenant/user/agent scope, which
-- the primary key indexes, so the search is exact. When the vector extension is installed and
-- visible on the search_path, the store computes the inner product with pgvector's operator
-- (real[] casts to vector); otherwise it uses plain SQL. An approximate (HNSW) index is
-- deliberately not created: its dimensions depend on the configured embedding model, and a
-- global approximate search filtered afterwards to one scope would miss that scope's records.
--
-- One vector per record, for the revision it was computed from: a correction replaces it, and
-- a deletion, an expiry purge or the removal of the record removes it.
CREATE TABLE IF NOT EXISTS algen_agent_runtime_learned_memory_vectors (
    tenant_id text NOT NULL,
    user_id text NOT NULL,
    agent_id text NOT NULL,
    key text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    -- The embedding identity (provider, model and version) that produced the vector.
    model text NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions > 0),
    embedding real[] NOT NULL CHECK (
        array_ndims(embedding) = 1 AND cardinality(embedding) = dimensions
    ),
    PRIMARY KEY (tenant_id, user_id, agent_id, key),
    FOREIGN KEY (tenant_id, user_id, agent_id, key)
        REFERENCES algen_agent_runtime_learned_memory (tenant_id, user_id, agent_id, key)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS algen_agent_runtime_learned_memory_vectors_model_idx
    ON algen_agent_runtime_learned_memory_vectors (tenant_id, user_id, agent_id, model);
