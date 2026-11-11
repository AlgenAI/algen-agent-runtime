CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS text_to_sql_schema_embeddings (
    tenant_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT,
    uri TEXT,
    chunk_index INTEGER NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(1536) NOT NULL,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS text_to_sql_schema_embeddings_document_idx
    ON text_to_sql_schema_embeddings (tenant_id, document_id);
CREATE INDEX IF NOT EXISTS text_to_sql_schema_embeddings_search_idx
    ON text_to_sql_schema_embeddings USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS text_to_sql_schema_embeddings_embedding_idx
    ON text_to_sql_schema_embeddings USING hnsw (embedding vector_cosine_ops);
