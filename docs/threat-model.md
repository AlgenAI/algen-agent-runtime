# Threat model and security checklist

## Assets and trust boundaries

Assets include tenant data, prompts, memories, credentials, tool authority, artifacts, and audit evidence. Untrusted inputs include users, retrieved content, model output, tool responses, URLs, plugin packages, and configuration overlays. Boundaries exist at API authentication, model providers, tools, storage, telemetry exporters, and plugin loading.

## Principal threats and controls

| Threat | Primary controls |
|---|---|
| Cross-tenant access | tenant IDs on every store lookup, authenticated principal, scoped API hooks, tool context set by the Runtime (never by tool arguments); adversarial tests drive a model at another tenant's runs, approvals, conversations, memory, retrieval, context artifacts and cache |
| Forged, stolen or replayed caller tokens | verified JWT/OIDC only outside loopback development, JWKS from allowlisted issuer hosts, asymmetric algorithms bound to key type, bounded clock skew, audience/issuer checks, fail-closed tenant mapping, short-lived tokens plus optional tenant-scoped revocation, content-minimal authentication audits |
| Spoofed remote service | per-host CA bundles, pinned server certificate fingerprints and client certificates (mTLS) in `network.remotes`, applied to every outbound client including through the egress proxy |
| Spoofed service identity | trusted-proxy identity off by default, honoured only from configured proxy CIDRs for exactly mapped identities, spoof attempts refused and audited, proxy-header peer rewriting disabled |
| Prompt injection | pre/post policy points (input and steering included), tool-result injection flag/block, document inspection, tool/model allowlists, centrally signed deny rules no agent can opt out of, approvals keyed to declared side effects (not arguments), model output never executes directly |
| Secret/PII leakage | secret references, redaction of key=value credentials, bearer tokens, emails, phone and card numbers at policy boundaries, in approval parameters, audit metadata, logs, runtime error text and opt-in telemetry content; content telemetry off, memory policy |
| SSRF & DNS rebinding | scheme/host allowlist, single-resolution DNS check, direct socket IP pinning, cloud metadata & private/link-local denial, fail-closed multi-IP validation, TLS SNI preservation, no redirects |
| Command execution | subprocess disabled by default, explicit permission and approval, empty environment |
| Replay/double side effect | durable call IDs (a reused id from the model is replaced), idempotency keys, completed-call checkpoints, approvals |
| Malicious plugin / hook provider | explicit module allowlist before import (`WorkflowHookLoader`, `PluginLoader` with `trusted_plugin_prefixes`), prefix-confusion defense, callable shape & API version checks, secret-free audit metadata |
| Prompt injection | pre/post policy points, tool/model allowlists, model output never executes directly |
| Secret/PII leakage | secret references, redaction middleware, content telemetry off, memory policy |
| SSRF & DNS rebinding | scheme/host allowlist, single-resolution DNS check, direct socket IP pinning, cloud metadata & private/link-local denial, fail-closed multi-IP validation, TLS SNI preservation, no redirects, optional egress proxy that receives only checked targets |
| Command execution | subprocess disabled by default, explicit permission and approval, empty environment, `security.sandbox` resource limits, optional isolation wrapper (`required: true` refuses to run without one), wall-clock limit and process-group kill |
| Replay/double side effect | durable call IDs, idempotency keys, completed-call checkpoints, approvals |
| Malicious plugin / hook provider | explicit module allowlist before import (`WorkflowHookLoader`, `PluginLoader` with `trusted_plugin_prefixes`), prefix-confusion defense, Ed25519 package signatures from `plugins.trusted_publishers` verified before import (`plugins.require_signed`), callable shape & API version checks, secret-free audit metadata |
| Resource exhaustion | payload/result/artifact limits, semaphores, API admission rate limits & in-flight run concurrency quotas, time/token/cost/step budgets |
| Unauthorized filesystem access | workspace root containment, path traversal and symlink-escape denial, starter tools disabled by default |
| Insecure code execution in tools | AST allowlist without eval(), operand/depth limits, subprocess disabled by default |
| Provider compromise | isolated adapters, raw response opt-in, egress policy, fallback/circuit breaker |
| Telemetry data exfiltration | content capture off, Traccia patching off by default, PII redaction, approved OTLP endpoints, sampling and retention policy |
| Untrusted MCP server / tool | operator tool allowlists, strict header/secret reference validation, DNS-pinned safe egress for remote transports (streamable_http/sse), untrusted annotations by default, operator policy precedence |
| MCP stdio subprocess execution | trusted operator configuration assumption (SR-12), environment allowlist, `security.sandbox` (or per-server `sandbox`) resource limits and isolation wrapper, new session with process-group kill on shutdown |
| Audit tampering | immutable audit contracts, append-only external sink, separate diagnostic logs |
| Tampered, stale or rolled-back policy | Ed25519-signed policy bundles verified at start-up against trusted keys, `require_signed`, signed version/issue/expiry checked (expired, future-dated and below-`min_version` bundles refused) |
| Policy engine failure or hang | explicit per-boundary fail mode (`closed` by default), per-policy evaluation timeout, `policy.error` audit records and metric for every handled error |

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
7. **Egress proxy (`network.egress_proxy`):**
   - Every client the Runtime builds (HTTP tools, `core.http`, `remote_tool`, MCP HTTP transports and OAuth, OIDC discovery and JWKS, workflow notifications, `HTTPModelService`, and the model providers) sends through the configured proxy, except hosts listed in `no_proxy`. `https://` targets use a CONNECT tunnel, so TLS (and any mTLS or pinning) stays end to end; `http://` targets are forwarded.
   - The target is checked first, exactly as for a direct connection (allowlist, scheme, resolution, private and metadata addresses), and a refused target never reaches the proxy. The proxied connection pool can connect only to the configured proxy host and port; the proxy may be private, because it is configured explicitly.
   - **Limit:** the proxy, not the Runtime, makes the final connection and resolves the name again, so DNS-rebinding pinning protects only up to the proxy. Configure the proxy to enforce its own egress policy (deny private, link-local and metadata ranges; allow only the destinations you expect). With `resolve_targets: false` the Runtime checks only the allowlist and IP literals and relies on the proxy entirely.
   - Proxy credentials are `env://` references sent as `Proxy-Authorization`; they never appear in the proxy URL, `repr`, logs or error messages. A `proxy=` passed to `create_safe_http_client` must be the configured proxy and `mounts=` is refused, so callers cannot route around the checks.
   - Not covered: the S3 client (boto3), OTLP and Traccia exporters, vector-store SDKs and other third-party clients keep their own proxy settings (usually `HTTPS_PROXY`).
8. **Remote service identity (`network.remotes`):** per host (or `*.domain`), a CA bundle that replaces the default trust store, a client certificate and key for mTLS, and SHA-256 fingerprints of accepted server leaf certificates (checked in addition to normal verification; a mismatch closes the connection with `PolicyDeniedError`). Keys come from files or `env://` references; an `env://` key is written to a private temporary file only while `ssl` loads it, and load errors never include key material.

### Plugin signatures

`PluginLoader` and the workflow service verify plugin and hook packages before importing them when `plugins.trusted_publishers` or `plugins.require_signed` is set. A signature (`algen-plugin-signature.json` in the package root) is Ed25519 over the publisher, the plugin name and version, the package name and the SHA-256 of every file in the package except bytecode caches, so a changed, added or removed file breaks it. The package is located with import finders without executing it. With `require_signed`, unsigned packages and packages from untrusted publishers are refused even when their prefix is trusted; after import the manifest must match the signed publisher, name and version. Limits: the signature does not cover dependencies or `__pycache__` bytecode (Python may load a cached `.pyc` whose recorded source size and time match), and a file changed after verification but before import is not detected, so install signed packages read-only. `PluginManifest.signature` is not used.

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
2. **Stdio Transport (Trusted Local Execution)**: Stdio servers execute operator-configured binaries as local host processes in their own session; on shutdown the whole process group is terminated. By default they inherit only `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `TEMP`, `TMP` and `SYSTEMROOT` plus the server's explicit `env`, with no other isolation. `security.sandbox` (or a server's own `sandbox`) narrows the environment to an explicit allowlist, sets POSIX resource limits (CPU seconds, address space, open files, processes, file size) and prefixes an isolation wrapper such as `bwrap`, `nsjail`, `firejail` or `sandbox-exec` for filesystem and network isolation; `security.sandbox.required: true` refuses to start a server without a wrapper. Without a wrapper a server can still read whatever the Runtime's user can, so stdio commands must remain trusted operator-declared executables.
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

[`examples/pattern_secured_api`](../examples/pattern_secured_api/README.md) exercises these controls
offline: OIDC verification against a JWKS, scope checks, conversation ownership, approval
redaction, token revocation, and a `caller` HTTP tool that fails closed without a credential.

### HTTP tools that forward the caller's credential

A declared HTTP tool with `auth.type: caller` sends the calling user's credential for the run to an
operator-declared API. The threats are a credential sent somewhere it should not go, a credential
that outlives its request, one user's response shown to another, and header injection. Controls:

- **Destination.** The scheme, host and path template are operator configuration, never model
  arguments, and the host must be in `security.allowed_http_hosts`; private networks stay blocked
  and redirects are never followed. Path values are model arguments: each is escaped as exactly
  one segment (`/`, `?`, `#` and `@` are encoded) and cannot be empty, `.` or `..`, also
  percent-encoded, so prompt injection cannot move the user's credential to another path on the
  host. A paginated call sends the credential only to the declared
  origin and fails when a response's `Link` header points elsewhere, even to an allowlisted host.
- **What is sent.** Only the one named credential, in the one configured header. Cookies and other
  caller headers are never forwarded, the header cannot be `Cookie`, `Host`,
  `Proxy-Authorization` or a framing or hop-by-hop header, and no `headers` template may set it, so
  model text never joins the value. Control characters anywhere in the value, a trailing line break
  included, are refused, so a value cannot add headers.
- **Audience.** A token is forwarded as is, so it must be one the target API is the audience of:
  the user's incoming `authorization` only when that API issued or accepts it for itself.
  Otherwise the host passes a token for that API under its own credential key, one key per API.
  Any `caller` tool can name any key in the run's credentials; which API receives which token is
  bound only by operator configuration.
- **Lifetime.** The credential is read from `ToolContext.credentials` at call time and follows its
  rules: memory only, gone when the run pauses or ends, and never in state, stores, events, audits,
  spans, logs or errors. Without it the call fails closed with `authentication required`; it never
  falls back to an unauthenticated request or to a deployment secret. Such a call, and one refused
  for an unsafe path value, is recorded `failed` rather than `indeterminate`: nothing was sent.
- **Cross-user results.** A call that carries caller credentials bypasses the tool-result cache,
  whose scope (tenant by default) is shared by several users. Code tools whose result depends on the
  caller's user, run or step without credentials declare it in `ToolDefinition.cache_context`,
  which adds those values (digested) to the cache key.
- **Response content.** The result keeps only a fixed set of response headers, so `Set-Cookie` and
  authentication challenges do not reach the model. The response body is the API's answer for that
  user and is treated like any tool result: untrusted data for the model.

### Policy distribution and failure

Policy rules distributed as [signed bundles](operations.md#signed-policy-bundles) are trusted only
as far as their signing key. The signature covers the bundle's whole file, metadata included, so
an attacker who can write the bundle directory cannot change a rule, extend an expiry, or put an
older signed version back without `min_version` refusing it. Keys come from `env://` or `file://`
references, never literals, and the private key never needs to be on a Runtime host. A signature
that does not verify is refused even when signing is not required. What remains trusted: whoever
can change `policies.trusted_keys` or `min_version` in configuration, and the Runtime host itself.

A policy that raises or hangs must not silently open a boundary. Each boundary's fail mode is
explicit and `closed` by default; `open` is a per-boundary decision for availability, skips only
the failed policy, and every handled error is audited and counted
([fail modes](operations.md#boundaries-and-fail-modes)).

### Adversarial coverage

`tests/adversarial` (pytest marker `adversarial`) runs scripted agent runs in normal CI, with a mock
model that obeys the attacker, and checks that the Runtime's controls hold end to end: injected
tool results, hidden document text, retrieved documents, input and steering; unapproved side
effects, parallel replies, reused tool-call ids and retried declined actions; credentials and
personal data in tool output, model output, tool arguments, errors, logs and telemetry; and a
model reaching for another tenant's runs, approvals, conversations, session memory, retrieval,
learned memory, context artifacts and cached results. Each test states its threat and control.
The suite runs against in-memory stores and deterministic patterns; it does not measure how well
a real model resists injection.

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
- Disable dynamic plugins unless packages are pinned, signed, scanned, and trusted; set
  `plugins.trusted_publishers` and `plugins.require_signed: true`, and install plugin packages
  read-only so nothing can change between verification and import.
- Route egress through `network.egress_proxy` with a proxy that enforces its own destination
  policy; use `network.remotes` for mTLS, private CAs and certificate pinning.
- Run MCP stdio servers and `core.subprocess` under `security.sandbox` with a wrapper and
  `required: true`.
- Require approval for write, external, and destructive tools.
- Distribute organisation-wide policy as signed bundles with `policies.require_signed: true`, keep
  the signing key out of Runtime hosts, and raise `min_version` with each bundle release.
- Keep `policies.on_error: closed`; set a boundary `open` only on purpose, and alert on
  `policy.error` audit records with `fail_open`. Set `policies.evaluation_timeout_seconds` when a
  policy calls a remote classifier.
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
