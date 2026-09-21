# Retrieval-augmented generation

## Vector-store backends

The `VectorStore` contract supports in-memory retrieval, pgvector, Chroma, FAISS, and Qdrant. Every
backend implements `ingest`, tenant-scoped `retrieve`, and tenant-scoped `delete`.

| Backend | Best suited to | Persistence | Hybrid retrieval |
|---|---|---:|---:|
| In-memory | Tests and small ephemeral deployments | No | Yes |
| pgvector | PostgreSQL-centric production systems | Yes | Vector + PostgreSQL full text |
| Chroma | Local development and embedded knowledge bases | Optional | Vector |
| FAISS | High-speed local indexes and offline workloads | Optional files | Vector |
| Qdrant | Dedicated scalable vector service | Yes | Vector with payload filters |

Persistent backends are populated explicitly through `ingest()`. YAML documents are limited to the
in-memory backend so startup cannot trigger costly or partially completed indexing jobs. Documents
should include `tenant_id` metadata.

```yaml
retrieval:
  knowledge:
    type: pgvector
    connection_url: env://PGVECTOR_DSN
    table_name: algen_agent_runtime_documents
    embedding_provider: openai
    embedding_model: text-embedding-3-small
    embedding_dimensions: 1536
    mode: hybrid
```

Install `algen-agent-runtime[pgvector]`, `[chroma]`, `[faiss]`, or `[qdrant]`. Index dimensions must match
the embedding model. Changing the model or dimensions requires a new collection or explicit re-index.

Chroma, FAISS, and Qdrant configurations must set `mode: vector`; requesting keyword or hybrid mode is
rejected rather than silently changing retrieval semantics. pgvector and the in-memory backend support
hybrid retrieval. Chroma uses persistent storage when `persistence_path` is set. FAISS persists an
index and metadata sidecar at `persistence_path`. Qdrant uses `connection_url`, `collection_name`, and
an optional `api_key: env://QDRANT_API_KEY`.

See `examples/text_to_sql_pgvector_agent` for schema ingestion, retrieval, citations, and grounded tool
execution.

Algen Agent Runtime implements RAG as composition rather than as a special agent type. A versioned agent
selects a named retrieval context builder; the builder queries a vendor-neutral retriever, packs
untrusted evidence into the model context, records stable citations, and the verifier rejects
missing or invented source identifiers.

```mermaid
flowchart LR
    D[Source documents] --> C[TextChunker]
    C --> E[Embedder]
    E --> I[DocumentIndexer]
    Q[Run request] --> P[Input policy]
    P --> R[Retriever]
    I --> R
    R --> F[Metadata and tenant filters]
    F --> H[Hybrid scoring]
    H --> RR[Optional reranker]
    RR --> B[RetrievalContextBuilder]
    B --> M[ModelProvider]
    M --> V[Citation verifier]
    V --> O[Response with sources]
```

## Configuration

```yaml
retrieval:
  handbook:
    type: memory
    mode: hybrid
    limit: 6
    minimum_score: 0.05
    keyword_weight: 0.4
    vector_weight: 0.6
    max_query_chars: 4096
    max_retrieval_tokens: 4000
    max_context_tokens: 16000
    chunk_size: 1200
    chunk_overlap: 200
    # Optional: use a configured model provider instead of local hashing.
    # embedding_provider: openai
    # embedding_model: text-embedding-3-small
    documents:
      - id: leave-policy
        title: Leave policy
        source: handbook://leave
        uri: https://knowledge.example/leave
        text: Full-time employees receive twenty days of annual leave.
        metadata: {tenant_id: tenant-a, category: people}

agents:
  - name: knowledge-assistant
    version: 1.0.0
    description: Grounded knowledge assistant
    capabilities: [rag, citations]
    system_instructions: Answer only from evidence and cite source IDs such as [S1].
    default_model: {name: primary, provider: openai, model: gpt-5}
    context_builder: retrieval.handbook
    planning_strategy: direct
    guardrail_policy: {policies: [secrets, authorization], require_citations: true}
    verification_policy: {verifiers: [non_empty, citations], max_repairs: 1}
```

Configuration documents are useful for examples and small static knowledge bases. Applications
can ingest and replace documents at runtime through the `DocumentIndexer` contract. Re-ingesting
a document replaces chunks with the same stable IDs. Tenant deletion only removes chunks owned
by that tenant.

## Retrieval behavior

- Keyword mode uses normalized token overlap.
- Vector mode uses cosine similarity.
- Hybrid mode combines normalized keyword and vector weights.
- The default `HashingEmbedder` is deterministic, local, and dependency-free. It is designed for
  testing and lightweight deployments, not semantic quality parity with a trained embedding model.
- `ModelProviderEmbedder` uses any configured provider implementing the normalized embedding
  contract.
- Static profile filters and request metadata named `retrieval_filter.<field>` narrow results.
- Set `RunRequest.metadata["retrieval_query"]` when the request input contains serialized workflow,
  semantic-layer, tool, or conversation context. The builder embeds this focused query while preserving
  the complete request input for the model. Retrieval query text is always capped by
  `max_query_chars` (4,096 by default), preventing oversized orchestration payloads from exceeding an
  embedding provider's input limit.
- Results are tenant filtered, deduplicated, deterministically ordered, optionally reranked, and
  packed to a retrieval token budget.

Documents are explicitly described to the model as untrusted data. Retrieved text must never be
interpreted as system instructions.

## Adding a vector database

Implement `Retriever.retrieve` and, for mutable indexes, `DocumentIndexer.ingest/delete`. Register
the adapter under a stable name in `RetrieverRegistry`, then register a `RetrievalContextBuilder`
with the desired configuration. Runtime, planning, verification, and response composition do not
need modification.

A production adapter should provide:

- Mandatory tenant predicates enforced in the datastore query
- Metadata filters using parameterized queries
- Batched embedding writes and retry-safe upserts
- Stable document and chunk identifiers
- Score normalization to the `[0, 1]` contract
- Timeouts, cancellation, rate limiting, and circuit breaking
- Encrypted connections and secret-provider integration
- Tenant-scoped deletion and retention jobs
- Retrieval latency, result count, and selected-source telemetry

The complete runnable example is in `examples/quickstart_rag` and supports both OpenAI and Mistral for
generation while using local deterministic embeddings by default.
