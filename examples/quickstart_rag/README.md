# RAG agent example

This example uses the same Algen Agent Runtime runtime as the text-to-SQL and workflow agents. The
documents, retrieval profile, provider, prompt, citation policy, and verifier are configuration;
there is no handbook-specific logic in the core package.

The default embedder is local and deterministic, so retrieval does not require a separate
embedding API. Generation uses OpenAI or Mistral:

```bash
export OPENAI_API_KEY=...
python -m examples.rag_agent.app "How much annual leave is available?"

export MISTRAL_API_KEY=...
python -m examples.rag_agent.app --provider mistral "When is support available?"
```

Documents without `metadata.tenant_id` are shared. Tenant-scoped documents are only visible to
that tenant. Per-request metadata keys prefixed with `retrieval_filter.` narrow configured
filters; they cannot widen tenant scope.

For production, register a `Retriever` adapter backed by the chosen vector database and a
`Reranker` implementation. Agent definitions continue to select a named context builder such as
`retrieval.company-handbook`, so orchestration and prompts do not depend on the store vendor.
