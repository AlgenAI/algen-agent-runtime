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
| `method`, `url` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE`. `{param}` placeholders in the path are filled in (escaped) and must be required parameters; the host cannot be templated. |
| `parameters` | JSON Schema (an object) for the arguments the model supplies. |
| `query`, `headers`, `body` | Templates. An entry naming a missing optional argument is left out. In `body`, a value that is exactly `"{param}"` keeps the argument's JSON type. `Authorization`, `Cookie` and `Host` headers are refused; use `auth`. |
| `auth` | `none`, `bearer` (`token`), `basic` (`username`, `password`) or `header` (`header_name`, `token`). Values are `env://` references, resolved per call. |
| `pagination` | `none`, `link_header` (follows `rel="next"`) or `page_param` (increments `page_param` from `start_page` until an empty page), up to `max_pages`; `items_pointer` names the list to concatenate. |
| `response` | `select`: a JSON pointer into the body; `output_schema`: validates the selected data. |
| `timeout_seconds`, `max_result_bytes` | Per-request limits; a larger response fails the call. |
| `side_effect`, `idempotency`, `required_permissions` | Defaults: `GET` is a read; other methods are external (and so need approval under the default approval policy). |

The tool returns `{"ok", "status", "headers", "data", "pages"}`. A non-2xx response is returned
with `ok: false` rather than raised, so the model can react to it.

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
