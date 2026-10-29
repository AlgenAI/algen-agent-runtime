# HTTP API tools

`AppSettings.http_tools` turns an HTTP API operation into an agent tool without code. Each
entry is one operation; the container registers it as a tool under its `name`.

```yaml
security:
  allowed_http_hosts: ["api.github.com"]
http_tools:
  - name: github.issue
    description: Read one GitHub issue.
    method: GET
    url: https://api.github.com/repos/{owner}/{repo}/issues/{number}
    parameters:
      type: object
      properties:
        owner: {type: string}
        repo: {type: string}
        number: {type: integer}
      required: [owner, repo, number]
    auth: {type: bearer, token: env://GITHUB_TOKEN}
    response: {select: /title}
```

| Field | Meaning |
| --- | --- |
| `name`, `description`, `version` | The tool's identity, as the model sees it. |
| `method`, `url` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE`. `{param}` placeholders in the path are filled in and must be required parameters; each value is escaped as exactly one segment and cannot be empty, `.` or `..` (also percent-encoded). The host cannot be templated. A query string in the URL is kept and sent before `query` and pagination parameters. |
| `parameters` | JSON Schema (an object) for the arguments the model supplies. |
| `query`, `headers`, `body` | Templates. An entry naming a missing optional argument is left out. In `body`, a value that is exactly `"{param}"` keeps the argument's JSON type. `Authorization`, `Cookie` and `Host` headers, and the `auth.header_name` of `caller` or `header` authentication, are refused; use `auth`. |
| `auth` | `none`, `bearer` (`token`), `basic` (`username`, `password`) or `header` (`header_name`, `token`): values are `env://` references, resolved per call. `caller`: the calling user's credential for this run (see below). |
| `pagination` | `none`, `link_header` (follows `rel="next"`) or `page_param` (increments `page_param` from `start_page` until an empty page), up to `max_pages`; `items_pointer` names the list to concatenate. |
| `response` | `select`: a JSON pointer into the body; `output_schema`: validates the selected data. |
| `timeout_seconds`, `max_result_bytes` | Per-request limits; a larger response fails the call. |
| `side_effect`, `idempotency`, `required_permissions` | Defaults: `GET` is a read; other methods are external (and so need approval under the default approval policy). |

The tool returns `{"ok", "status", "headers", "data", "pages"}`. A non-2xx response is returned
with `ok: false` rather than raised, so the model can react to it.

## Calling the API as the signed-in user

`auth.type: caller` sends the credential the host passed for this run, instead of one deployment
secret, so the API sees the signed-in user and applies that user's permissions:

```yaml
http_tools:
  - name: lms.my_courses
    description: List the courses the signed-in user is enrolled in.
    url: https://lms.example.com/api/me/courses
    auth:
      type: caller
      credential: authorization   # key in the run's credentials (default)
      # header_name: Authorization  (default)
      # scheme: Bearer              (optional; prefixed unless the value already has it)
      # Prefer a key per API, e.g. credential: lms_token, over one shared "authorization".
```

```python
await runtime.run(request, credentials={"authorization": incoming_headers["authorization"]})
```

- Forward a token only to an API that is its audience: the host's own incoming `authorization`
  is right when the target API issued it, or accepts it, for itself (the LMS signed the user in).
  Otherwise pass a token issued for that API under its own key (`credentials={"lms_token": ...}`
  with `credential: lms_token`), one key per API. Any `caller` tool can name any key in the run's
  credentials, so which API receives which token is decided by this configuration alone.
- The value comes from `ToolContext.credentials[credential]`
  ([credentials of the caller](tool-authoring.md#credentials-of-the-caller)). It is sent only as
  `header_name`: never as `Cookie`, `Set-Cookie`, `Host`, `Proxy-Authorization` or a framing
  header, which settings validation refuses. No other credential, cookie or request header of the
  caller is forwarded, and `token`, `username` and `password` are refused with `caller`.
- A call without that credential fails with `authentication required` before any request is sent.
  This is the case for a run started without credentials, a run resumed without them, a run recovered
  after a restart, workflow tool steps and runs started through the HTTP API, which pass none. A
  blank value, or with `scheme` set a value that is only the scheme (`"Bearer "`), counts as none.
  The tool-execution ledger records such a call `failed`, not `indeterminate`, even for a write:
  nothing was sent, so the same call runs normally once the user signs in again.
- A value with a control character anywhere, a trailing line break included, is refused without
  being echoed. With `scheme` set, a value that carries another scheme (`Basic ...`) or more than
  one word is refused instead of being prefixed.
- The credential goes only to the declared URL's origin (scheme, host and port). A `Link` header
  that points the next page at another origin fails the call instead of forwarding the credential,
  even to an allowlisted host. Redirects are never followed.
- A call that carries caller credentials is never served from, or stored in, the tool-result cache
  (`cache.policies.tool_results`), whose scope is shared by more than one caller.
- The credential is not part of the tool's arguments, result, events, audits, spans or errors;
  response headers are limited to `content-type`, `etag`, `x-ratelimit-remaining` and
  `retry-after`, so a `Set-Cookie` from the API never reaches the model.

## Safety

- Requests go through `create_safe_http_client`: DNS-rebinding and SSRF protection, private
  networks blocked unless `security.allow_private_networks`, and redirects never followed.
- The URL's host must be listed in `security.allowed_http_hosts` (an exact host or a parent
  domain). Settings validation refuses a declared tool whose host is not listed, even though an
  empty allowlist otherwise admits every host.
- Every page's URL, including `Link` header targets, is checked against the allowlist.
- Secrets never appear in settings or tool arguments, only `env://` references.

## Workflow tool steps

A workflow `handler` node can call any registered tool, including an HTTP API tool, with
`handler: tool:<name>` and `arguments` (see [multi-agent workflows](multi-agent-workflows.md)).
