# Threat model and security checklist

## Assets and trust boundaries

Assets include tenant data, prompts, memories, credentials, tool authority, artifacts, and audit evidence. Untrusted inputs include users, retrieved content, model output, tool responses, URLs, plugin packages, and configuration overlays. Boundaries exist at API authentication, model providers, tools, storage, telemetry exporters, and plugin loading.

## Principal threats and controls

| Threat | Primary controls |
|---|---|
| Cross-tenant access | tenant IDs on every store lookup, authenticated principal, scoped API hooks |
| Forged, stolen or replayed caller tokens | verified JWT/OIDC only outside loopback development, JWKS from allowlisted issuer hosts, asymmetric algorithms bound to key type, bounded clock skew, audience/issuer checks, fail-closed tenant mapping, short-lived tokens plus optional tenant-scoped revocation, content-minimal authentication audits |
| Spoofed service identity | trusted-proxy identity off by default, honoured only from configured proxy CIDRs for exactly mapped identities, spoof attempts refused and audited, proxy-header peer rewriting disabled |
| Prompt injection | pre/post policy points, tool/model allowlists, model output never executes directly |
| Secret/PII leakage | secret references, redaction middleware, content telemetry off, memory policy |
| SSRF & DNS rebinding | scheme/host allowlist, single-resolution DNS check, direct socket IP pinning, cloud metadata & private/link-local denial, fail-closed multi-IP validation, TLS SNI preservation, no redirects |
| Command execution | subprocess disabled by default, explicit permission and approval, empty environment |
| Replay/double side effect | durable call IDs, idempotency keys, completed-call checkpoints, approvals |
| Malicious plugin / hook provider | explicit module allowlist before import (`WorkflowHookLoader`, `PluginLoader` with `trusted_plugin_prefixes`), prefix-confusion defense, callable shape & API version checks, secret-free audit metadata |
| Resource exhaustion | payload/result/artifact limits, semaphores, API admission rate limits & in-flight run concurrency quotas, time/token/cost/step budgets |
| Unauthorized filesystem access | workspace root containment, path traversal and symlink-escape denial, starter tools disabled by default |
| Insecure code execution in tools | AST allowlist without eval(), operand/depth limits, subprocess disabled by default |
| Provider compromise | isolated adapters, raw response opt-in, egress policy, fallback/circuit breaker |
| Telemetry data exfiltration | content capture off, Traccia patching off by default, PII redaction, approved OTLP endpoints, sampling and retention policy |
| Untrusted MCP server / tool | operator tool allowlists, strict header/secret reference validation, DNS-pinned safe egress for remote transports (streamable_http/sse), untrusted annotations by default, operator policy precedence |
| MCP stdio subprocess execution | trusted operator configuration assumption (SR-12), minimal system environment filtering, process isolation deferred |
| Audit tampering | immutable audit contracts, append-only external sink, separate diagnostic logs |

### Outbound Network Security and SSRF / DNS Rebinding Mitigation

All outbound HTTP calls initiated by runtime components (`core.http`, `remote_tool`, and `HTTPModelService`) enforce strict transport-level protections against Server-Side Request Forgery (SSRF) and DNS rebinding Time-of-Check-Time-of-Use (TOCTOU) vulnerabilities:

1. **Elimination of DNS Rebinding TOCTOU:**
   - Standard URL validation resolves DNS, verifies that the resulting IP is public, and then passes the URL to an HTTP client. Vulnerable clients perform a *second* DNS resolution during TCP socket establishment, allowing malicious DNS servers (with 0-second TTLs) to return a private or metadata address on the second lookup.
   - The runtime's `SafeNetworkBackend` and `SafeAsyncTransport` resolve DNS records **once** in worker threads and bind the underlying TCP socket directly to a pre-validated IP address. No secondary DNS resolution occurs at the transport or OS socket layer.
2. **TLS SNI and Certificate Verification Preservation:**
   - Connecting directly to an IP address typically causes TLS certificate validation to fail because the server certificate matches the domain name rather than the IP.
   - The runtime overrides `start_tls` to pass the original requested hostname as `server_hostname`. This guarantees that TLS Server Name Indication (SNI) and X.509 certificate trust checks proceed normally while the underlying TCP socket remains pinned to the validated IP.
3. **Comprehensive IP Deny Matrix:**
   - By default, all outbound connections reject:
     - **Private networks:** RFC 1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
     - **Loopback addresses:** `127.0.0.0/8`, `::1`
     - **Link-local addresses:** RFC 3927 (`169.254.0.0/16`, `fe80::/10`)
     - **Carrier-grade NAT:** RFC 6598 (`100.64.0.0/10`)
     - **Cloud instance metadata services (IMDS):** AWS/GCP/Azure (`169.254.169.254`), AWS ECS task metadata (`169.254.170.2`), AWS IMDSv6 (`[fd00:ec2::254]`), Alibaba Cloud (`100.100.100.200`)
     - **Unspecified / broadcast / multicast:** `0.0.0.0`, `::`, `224.0.0.0/4`, `ff00::/8`
     - **IPv4-mapped IPv6:** `::ffff:0:0/96` mapping back to forbidden IPv4 ranges.
4. **Fail-Closed Multi-Address Validation:**
   - If a hostname resolves to multiple IPv4 or IPv6 records (e.g. dual-homed or round-robin DNS), **every** resolved candidate IP must be public and permitted. If any resolved IP is private or forbidden, the entire request is immediately rejected.
5. **URL Normalization & Host Allowlists:**
   - Schemes are strictly restricted to `http` and `https`.
   - Embedded user credentials (`http://user:pass@host`) are explicitly forbidden.
   - Percent-encoded host components (e.g. `%31%32%37...`) are decoded and evaluated.
   - Host allowlists support exact hostnames and domain suffix wildcards (`example.com` matches `sub.example.com`).
6. **Controlled Override & Defense in Depth:**
   - `allow_private_networks=True` can be passed to selectively permit private/loopback networks for local development harnesses or internal VPC deployments.
   - In production environments, application-level IP binding should be paired with a deployment-level egress firewall or forward proxy as defense in depth.

### Hook Provider Loading & Dynamic Import Controls

Workflow manifests specify hook providers as dotted paths (`module:callable`). To prevent arbitrary code execution via untrusted YAML manifests or compromised configurations:

1. **Authorization Before Import:**
   - `WorkflowHookLoader` validates module authorization strictly *before* calling `importlib.import_module()`. This prevents top-level module code from executing during unauthorized imports.
2. **Explicit Host Module Allowlists:**
   - Hosts must explicitly declare trusted module prefixes (`allowed_modules`). An empty allowlist rejects all dynamic imports with `PolicyDeniedError`.
3. **Prefix Confusion Defense:**
   - Allowlist matching strictly compares exact module names and dot-separated package segments (`module == allowed or module.startswith(allowed + ".")`). Simple string prefixes that would allow `example.test_bypass` when `example.test` is allowed are explicitly rejected.
4. **Callable Shape & Version Verification:**
   - The loader verifies that the resolved factory is callable, adheres to supported API versions (`__api_version__` in `{"1", "1.0", "v1"}`), and returns an instance of `WorkflowHookRegistry`.
5. **Secret-Free Audit Metadata:**
   - Loaded providers produce structured audit metadata (`LoadedHookProvider.audit_metadata`) recording module, factory, and package version without serializing executable code or environment secrets.

### Request Payload Wire Limits

To prevent memory exhaustion and denial of service through unconstrained request bodies (whether sent with accurate headers, omitted headers, or chunked transfer encoding), incoming HTTP requests pass through pure ASGI `RequestBodyLimitMiddleware`:

1. **Header validation:** Invalid `Content-Length` headers (negative, non-integer, or duplicate) are rejected immediately with HTTP 400 (`INVALID_CONTENT_LENGTH`).
2. **Pre-read bounds:** Declared sizes exceeding `security.max_request_bytes` (or `max_artifact_bytes` for artifact uploads) are rejected immediately with HTTP 413 (`REQUEST_PAYLOAD_TOO_LARGE`) before reading body content.
3. **Stream counting:** Regardless of headers, chunks are counted as they arrive from ASGI `receive`. If accumulated wire bytes exceed the limit, execution aborts with HTTP 413, guaranteeing the server never buffers beyond configured bounds.
4. **Information leakage prevention:** 413 and 400 error responses never echo request content and provide stable error codes.

### Model Context Protocol (MCP) Security & Trust Boundaries

External MCP servers integrate into Runtime under explicit security boundaries:

1. **Client-Only Architecture**: Runtime functions exclusively as an MCP client consuming tools from external servers; it does not host an MCP server.
2. **Stdio Transport (Trusted Local Execution)**: Stdio servers execute operator-configured binaries as unsandboxed local host processes with no kernel sandbox, resource cgroups, or signature verification. Ambient process environment variables are filtered to a minimal safe set (`PATH`, `SYSTEMROOT`, `TEMP`, `USER`, `HOME`) to reduce accidental secret leakage, but stdio commands must only execute trusted operator-declared executables.
3. **Remote Transports & Outbound Egress**: Streamable HTTP and SSE transports use `_make_http_client_factory` backed by `SafeAsyncTransport` and `SafeNetworkBackend`, enforcing DNS-pinned single-resolution IP validation, host allowlists (`security.allowed_http_hosts`), and private IP blocking (`security.allow_private_networks`).
4. **Secret Hygiene**: Literal sensitive headers (`Authorization`, `Proxy-Authorization`, `Cookie`, `X-API-Key`, etc.) are rejected at configuration validation time. Authentication must use `auth_token_ref` or `secret_headers` with `env://` references.
5. **Server Annotation Trust Model**: Server-provided annotations (`ToolAnnotations`) are treated as untrusted hints by default (`trust_tool_annotations: false`). Runtime defaults tools to `SideEffect.EXTERNAL` and `Idempotency.NON_IDEMPOTENT` unless the operator explicitly trusts annotations or provides authoritative `tool_policies` overrides.

### Per-run caller credentials

Credentials a host passes to `Runtime.start`, `run` or `resume` (for example the signed-in user's bearer token, for tools that act as that user) are kept only in the process's memory, keyed by run, and only while the run executes. They are never written to `RunState`, checkpoints, the run, approval, event or audit stores, events, audits, spans, logs, caches or the model's context, and `ToolContext` leaves them out of its `repr`, serialization, equality and hashing, so a store, exporter, log line or traceback cannot hold them. They are dropped when the run pauses for approval or clarification and when it ends, however it ends. A pause can last longer than a token lives, and a persisted token would outlive the request that carried it and become readable to anyone with access to the stores, so the host must supply fresh credentials on `resume`; a run resumed without them, or recovered after a restart, gives its tools an empty mapping and they should refuse to act. Concurrent runs never share credentials; a child run started by `core.subagent` holds its own copy of its parent's until it ends. Tools receive a read-only copy and must not echo it in results or errors, which do reach the model, events and stores.

### API authentication

Callers authenticate with one of three modes (docs/operations.md#api-authentication).
`development_headers` trusts identity headers and binds only to a loopback host unless an
operator explicitly opts out for an isolated development network. `jwt` verifies bearer tokens
with one static `env://` key. `oidc` verifies bearer tokens against each configured issuer's
JWKS:

- **Key retrieval is an egress path.** Discovery and JWKS fetches go through the SSRF-safe client,
  restricted to the issuer host, a configured `jwks_url` host and explicitly listed extra hosts;
  private networks stay closed unless configured, redirects are not followed, bodies are capped,
  and a discovery document must name the configured issuer. A `jwks_uri` on another host is
  never fetched.
- **Key-id DoS.** An unknown `kid` refreshes the JWKS at most once per
  `jwks_min_refresh_interval_seconds` per issuer; floods of random key ids cost no upstream
  traffic. A failed refresh keeps the last good keys, so an identity-provider outage does not
  lock out callers whose keys are cached.
- **Algorithm confusion.** Only configured asymmetric algorithms are accepted (`none` and `HS*`
  are rejected at configuration time), the algorithm must match the key type and any `alg` the
  JWKS states, and symmetric JWKS keys are ignored, so a token "signed" with the public key as an
  HMAC secret is refused.
- **Claims.** `exp`, `iss`, `aud` and the subject are required, `nbf`/`iat` are checked, skew is
  bounded (at most 300 seconds), and `max_token_lifetime_seconds` can refuse long-lived tokens.
  The tenant comes from a configured claim or a fixed tenant per issuer and is never defaulted;
  a token's own `*` scope is dropped.
- **Revocation.** Short-lived tokens are the primary control. The optional check denies a `jti`
  until its expiry or every token for a subject issued before a cutoff. Entries are
  tenant-scoped, so one tenant's administrator cannot revoke another tenant's tokens; a store
  outage fails closed. Introspection (RFC 7662) is deferred.
- **Service identity.** A client-certificate identity forwarded by a TLS-terminating proxy is
  honoured only from configured proxy CIDRs (never every address), only for exactly mapped
  identities, and the same header from any other peer is refused and audited. The proxy must
  overwrite the header; `serve` disables `X-Forwarded-For` peer rewriting so a forwarded address
  cannot impersonate the proxy.
- **Audit.** Every failure (and, opt-in, every success) is recorded with at most the method,
  reason, issuer, key id and verified subject, never the token. Failures without a verified
  tenant go to the `_authentication` audit tenant, so unverified claims cannot write into a
  tenant's trail, and a per-minute cap keeps a credential flood from flooding the audit store.

## Deployment checklist

- Use `oidc` (or `jwt`) authentication outside loopback development; require short-lived tokens
  (`max_token_lifetime_seconds`), map tenants explicitly, and enable `token_revocation` with the
  `postgres` store when tokens must be revocable before expiry.
- Enable `trusted_proxy_identity` only behind a proxy that verifies client certificates and
  overwrites the identity header; list only the proxies' own CIDRs.
- Store secrets in a managed secret provider; never in YAML.
- Set explicit egress and tool host allowlists.
- Verify all declared `mcp_servers` stdio commands and args are strictly trusted operator-authored code; review remote MCP server URLs against egress host allowlists.
- Enforce `WorkflowHookLoader` with explicit module allowlists for dynamic workflow manifests.
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
- Enable `api.rate_limiting` to guard against single-node run, approval, and conversation floods; use an API gateway or reverse proxy for cross-worker distributed rate limiting when running multi-worker clusters.
- Test cancellation, restore, approval expiry, incident redaction, and audit delivery.
