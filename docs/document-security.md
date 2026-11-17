# Untrusted document inspection

The document extractors moved to
[algen-agent-tools](https://github.com/AlgenAI/algen-agent-tools) and were removed from the Runtime
in 0.2, after warning since 0.1.0a19. Install `algen-agent-tools[documents]` (the extra brings
PyMuPDF for PDFs) and change the import:

```python
# Before (0.1)
from algen_agent_runtime.security.documents import extractor_for, redact_pii

# 0.2
from algen_agent_tools.documents.inspection import extractor_for, redact_pii
```

The names are the same: `extractor_for`, `ExtractedDocument`, `DocumentSpan`,
`DocumentSecurityFinding`, `DocumentDisposition`, `FindingSeverity`, `sanitize_untrusted_text`,
`redact_pii`, `prompt_injection_findings`, `classify_findings` and the per-format extractors.
The Runtime's `documents` extra is gone too.

What the Runtime still checks itself:

- **Run attachments.** The type and size checks on files attached to a run do not depend on the
  extractors (see [Attachments](attachments.md)).
- **Tool results.** Prompt-injection screening of tool results (`guardrail_policy.tool_result_injection`:
  `off`, `flag` or `block`, off by default) and the trust marking of what a tool returns stay in
  the Runtime, so a tool that reads documents with the extractors is governed like any other.
- **Retrieved evidence.** From 0.2.0b3, `guardrail_policy.retrieval_injection` (`flag` or
  `drop`, off by default) screens retrieved chunks with the same check as tool results before the
  model reads them.
- **PII and credential masking.** `redact_pii` and `redact_secrets` (since 0.2.0b3) and the
  lower-level `is_sensitive_key`, `redact_secret_text` and `fold_text` (since 0.2.0b4) are public
  in `algen_agent_runtime.security`, so a tool or host masks extracted text with the Runtime's own
  rules (see [reusing the redaction rules](agent-tooling.md#reusing-the-runtimes-redaction)).
  `algen_agent_tools.documents.inspection.redact_pii` is the tools package's own copy.
