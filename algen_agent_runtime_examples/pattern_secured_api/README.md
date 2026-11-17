# Secured HTTP API (OIDC)

The Runtime's HTTP API with `api.auth_mode: oidc`, served in-process: the FastAPI app runs through
`httpx.ASGITransport`, the identity provider (a discovery document and a JWKS) and an upstream LMS
are `httpx.MockTransport` stubs (`fakes.py`), and the EC P-256 signing key is generated when the
example starts. No network, no API key, and no secret in the configuration.

Three users of tenant `acme`: alice and bob (role `member`) and carol (role `approver`, plus the
`auth:revoke` and `artifacts:admin` scopes in her token's `scope` claim). The example shows:

1. **Scopes from claims.** `api.oidc.issuers[].claims` maps the `roles` claim through
   `role_scopes` and grants the `scope` claim directly. A request without a token, or with a token
   signed by another key under the provider's key id, gets 401.
2. **An approval pause and the approval inbox.** alice starts a run whose tool,
   `webhooks.register` (`tools.py`, registered by `WebhookPlugin`), needs approval. `GET /v1/approvals` needs `approvals:read`: alice gets 403,
   carol sees the pending request with the proposed `secret` shown as `[REDACTED len=24 sha256=…]` (its length and a digest prefix, so carol can tell it was changed without seeing it). carol approves
   with `modified_parameters` carrying a new secret; the inbox shows it redacted too.
3. **Conversation and file ownership.** alice's conversation turn runs as a run. alice can read
   it; bob, in the same tenant, gets 404 for the run and the conversation. alice uploads a file
   (`POST /v1/artifacts`); since 0.2.0b4 it is hers: bob gets 404 (the same answer as for a
   missing file), and carol reads it with `artifacts:admin`. Grant `artifacts:admin` to operator
   tools only.
4. **Calling an API as the signed-in user.** The `lms.my_courses` entry in `http_tools` uses
   `auth.type: caller`. The HTTP API's run endpoints pass no caller credentials, so the example adds
   a host route (`POST /host/my-courses`) that authenticates the request the same way and calls
   `runtime.run(..., credentials={"authorization": <the request's Authorization header>})`; the
   fake LMS verifies the token and answers for alice. The same agent started through
   `POST /v1/runs` fails closed with `authentication required`, and the LMS receives nothing.
5. **Token revocation.** With `api.token_revocation` enabled, carol revokes bob's token by its
   `jti` (`POST /v1/auth/revocations/tokens`); bob's next request gets 401 and alice's still works.

## Run it

```bash
pip install 'algen-agent-runtime[auth]'   # PyJWT and cryptography
python -m algen_agent_runtime_examples.pattern_secured_api.app
```

From a source checkout: `pip install -e '.[dev,auth]'`, or without installing,
`PYTHONPATH=src:. python -m algen_agent_runtime_examples.pattern_secured_api.app`.

Without the `auth` extra the example prints

```text
SKIPPED: pattern_secured_api needs the auth extra (PyJWT and cryptography) to sign and verify tokens. Install it with: pip install 'algen-agent-runtime[auth]'
```

and exits 0.

## Expected output

The walkthrough goes to stdout. The Runtime's own JSON log lines (refused tokens, the failed run in
step 4) go to stderr.

```text
== 1. Tokens and scopes from claims ==
alice: user=alice scopes=['conversations:read', 'conversations:write', 'runs:read', 'runs:write']
bob: user=bob scopes=['conversations:read', 'conversations:write', 'runs:read', 'runs:write']
carol: user=carol scopes=['approvals:decide', 'approvals:read', 'artifacts:admin', 'auth:revoke', 'runs:read', 'runs:write']
no token -> 401
token signed by another key -> 401

== 2. An approval pause and the approval inbox ==
alice starts a run -> awaiting_approval
alice GET /v1/approvals -> 403 (missing scope approvals:read)
carol GET /v1/approvals -> 1 pending: Execute webhooks.register {'url': 'https://hooks.acme.example/billing', 'secret': '[REDACTED len=24 sha256=16cd199d]'}
carol modifies and approves -> 202; run completed
inbox shows modified with {'url': 'https://hooks.acme.example/billing', 'secret': '[REDACTED len=24 sha256=f03d220e]'}

== 3. Conversation and file ownership ==
alice GET /v1/runs/<her turn's run> -> 200
bob GET /v1/runs/<alice's turn's run> -> 404
bob GET /v1/conversations/<alice's> -> 404
alice POST /v1/artifacts -> 201
GET /v1/artifacts/<alice's file>/metadata -> alice 200, bob 404, carol (artifacts:admin) 200

== 4. Calling the LMS as the signed-in user (auth.type: caller) ==
host route runs course-assistant with alice's credential -> completed: You have two courses.
LMS saw: ['alice']
POST /v1/runs (no caller credential) -> failed: lms.my_courses: authentication required: the caller's 'authorization' credential was not passed for this run; sign in again and retry
requests the LMS received for it: 0

== 5. Token revocation ==
bob before -> 200
carol POST /v1/auth/revocations/tokens (bob's jti) -> 200
bob after -> 401; alice -> 200
identity provider requests: 4 (discovery and JWKS once each for the API and the host route, then cached)
```

## What is simulated, and what a deployment does instead

- **The identity provider.** A deployment points `issuer` at its real IdP over https and does not
  inject a transport; JWKS fetches then go through the SSRF-safe client. Keep
  `max_token_lifetime_seconds` short: short-lived tokens are the main revocation control.
- **DNS for the LMS host.** `lms.example.com` does not exist, so during step 4 the example replaces
  only the DNS step of the outbound check (`offline_dns` in `app.py`); the URL and
  `security.allowed_http_hosts` checks still run. A deployment never does this.
- **One token for two audiences.** The demo tokens name both the Runtime and the LMS as audiences,
  so forwarding the incoming `Authorization` header is correct. When the target API is not an
  audience of the user's Runtime token, pass a token issued for it under its own key
  (`credentials={"lms_token": ...}` with `auth.credential: lms_token`).
- **Revocation storage.** `token_revocation.store: memory` is per process and lost on restart; use
  `postgres` in a deployment, so revocations survive restarts (and are shared by every process if
  you run more than one, which is experimental in the 1.0 single-node scope).
- The other stores are in memory too; see the durable configuration in the operations guide.

## Read next

- [API authentication](../../docs/operations.md#api-authentication): OIDC issuers and JWKS, claim
  mapping, token revocation, authentication audit events
- [Approval inbox](../../docs/operations.md#approval-inbox)
- [Calling the API as the signed-in user](../../docs/http-tools.md#calling-the-api-as-the-signed-in-user)
- [Threat model](../../docs/threat-model.md): API authentication, per-run caller credentials and
  HTTP tools that forward the caller's credential
