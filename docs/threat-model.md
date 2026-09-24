# Threat model and security checklist

## Assets and trust boundaries

Assets include tenant data, prompts, memories, credentials, tool authority, artifacts, and audit evidence. Untrusted inputs include users, retrieved content, model output, tool responses, URLs, plugin packages, and configuration overlays. Boundaries exist at API authentication, model providers, tools, storage, telemetry exporters, and plugin loading.

## Principal threats and controls

| Threat | Primary controls |
|---|---|
| Cross-tenant access | tenant IDs on every store lookup, authenticated principal, scoped API hooks |
| Prompt injection | pre/post policy points, tool/model allowlists, model output never executes directly |
| Secret/PII leakage | secret references, redaction middleware, content telemetry off, memory policy |
| SSRF & DNS rebinding | scheme/host allowlist, single-resolution DNS check, direct socket IP pinning, cloud metadata & private/link-local denial, fail-closed multi-IP validation, TLS SNI preservation, no redirects |
| Command execution | subprocess disabled by default, explicit permission and approval, empty environment |
| Replay/double side effect | durable call IDs, idempotency keys, completed-call checkpoints, approvals |
| Malicious plugin / hook provider | explicit module allowlist before import (`WorkflowHookLoader`), prefix-confusion defense, callable shape & API version checks, secret-free audit metadata |
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

## Deployment checklist

- Replace header identity hook with verified JWT or mTLS and deny missing tenant claims.
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
