# Threat model and security checklist

## Assets and trust boundaries

Assets include tenant data, prompts, memories, credentials, tool authority, artifacts, and audit evidence. Untrusted inputs include users, retrieved content, model output, tool responses, URLs, plugin packages, and configuration overlays. Boundaries exist at API authentication, model providers, tools, storage, telemetry exporters, and plugin loading.

## Principal threats and controls

| Threat | Primary controls |
|---|---|
| Cross-tenant access | tenant IDs on every store lookup, authenticated principal, scoped API hooks |
| Prompt injection | pre/post policy points, tool/model allowlists, model output never executes directly |
| Secret/PII leakage | secret references, redaction middleware, content telemetry off, memory policy |
| SSRF | scheme/host allowlist, DNS resolution checks, private/link-local denial, no redirects |
| Command execution | subprocess disabled by default, explicit permission and approval, empty environment |
| Replay/double side effect | durable call IDs, idempotency keys, completed-call checkpoints, approvals |
| Malicious plugin | trusted namespace, entry-point-only discovery, API version validation, deployment signing policy |
| Resource exhaustion | payload/result/artifact limits, semaphores, rate limits, time/token/cost/step budgets |
| Provider compromise | isolated adapters, raw response opt-in, egress policy, fallback/circuit breaker |
| Telemetry data exfiltration | content capture off, Traccia patching off by default, PII redaction, approved OTLP endpoints, sampling and retention policy |
| Audit tampering | immutable audit contracts, append-only external sink, separate diagnostic logs |

## Deployment checklist

- Replace header identity hook with verified JWT or mTLS and deny missing tenant claims.
- Store secrets in a managed secret provider; never in YAML.
- Set explicit egress and tool host allowlists.
- Disable dynamic plugins unless packages are pinned, signed, scanned, and trusted.
- Require approval for write, external, and destructive tools.
- Configure encryption at rest/in transit and tenant-aware database row security.
- Disable raw provider metadata and content telemetry unless explicitly reviewed.
- Treat Traccia and secondary OTLP destinations as data processors; review residency, retention, endpoint allowlists, SDK upgrades, and any automatic instrumentation before enabling them.
- Set retention, deletion, artifact-size, request-size, and memory-size limits.
- Treat uploaded artifacts as untrusted: keep them `pending_scan`, deny public download or workflow
  consumption until a deployment-owned scanner marks them `available`, and isolate quarantined data.
- Treat email as controlled data egress: validate addresses and headers against injection, cap
  recipients and artifact attachments, keep BCC out of headers, require TLS, require policy/approval
  where appropriate, and never replay an ambiguous send without provider reconciliation.
- Run dependency, container, and plugin supply-chain scanning.
- Test cancellation, restore, approval expiry, incident redaction, and audit delivery.
