"""Declarative HTTP API tools: an API call described in settings becomes an agent tool.

Each ``http_tools`` entry names one operation: method, a URL template with ``{param}``
placeholders, query, header and body templates, the parameters (JSON Schema) the model fills
in, authentication (``env://`` secret references, or the caller's per-run credentials),
pagination, limits and how to pick the part of the response the model sees. Calls go through
``create_safe_http_client``: the URL's host must be in ``security.allowed_http_hosts``, private
networks stay blocked unless allowed, and redirects are never followed. A non-2xx response is
returned to the model as ``ok: false``, not raised, so it can react to it.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from algen_agent_runtime.exceptions.errors import ToolExecutionError, ToolNotAttemptedError
from algen_agent_runtime.security.network import create_safe_http_client, validate_outbound_url
from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)

SecretResolver = Callable[[str], Awaitable[str]]
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_WHOLE_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?', re.IGNORECASE)
_KEPT_HEADERS = frozenset({"content-type", "etag", "x-ratelimit-remaining", "retry-after"})
# Headers a caller credential may never be sent as.
_RESERVED_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
    }
)
_HEADER_VALUE = re.compile(r"^[\x20-\x7e\t]+$")
_BLANKS = " \t"
# Path values that would change the path itself once escaped (dot segments) or empty a segment.
_UNSAFE_SEGMENTS = frozenset({"", ".", ".."})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HttpToolAuth(_Strict):
    """How a declared HTTP tool authenticates.

    ``bearer``, ``basic`` and ``header`` read ``env://`` secret references: the same credential
    for every run. ``caller`` forwards the calling user's credential for this run: the value under
    ``credential`` in ``ToolContext.credentials`` (what the host passed to ``Runtime.run``,
    ``start`` or ``resume``), sent as ``header_name`` (default ``Authorization``), prefixed with
    ``scheme`` when one is set. A call without that credential fails before any request is sent.
    """

    type: Literal["none", "bearer", "basic", "header", "caller"] = "none"
    # env:// references only; resolved when the tool is called, never stored in settings.
    token: str | None = None
    username: str | None = None
    password: str | None = None
    header_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")
    # caller: the key in ToolContext.credentials, and an optional scheme such as "Bearer".
    credential: str = Field(default="authorization", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    scheme: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9-]{0,31}$")

    @model_validator(mode="after")
    def complete_and_referenced(self) -> HttpToolAuth:
        if self.type == "caller":
            for field in ("token", "username", "password"):
                if getattr(self, field):
                    raise ValueError(f"caller authentication takes no {field}")
            if self.header_name and self.header_name.lower() in _RESERVED_HEADERS:
                raise ValueError(f"caller authentication cannot be sent as {self.header_name!r}")
            return self
        if self.scheme is not None:
            raise ValueError("scheme applies to caller authentication only")
        needed = {
            "none": (),
            "bearer": ("token",),
            "basic": ("username", "password"),
            "header": ("header_name", "token"),
        }[self.type]
        for field in needed:
            if not getattr(self, field):
                raise ValueError(f"{self.type} authentication requires {field}")
        for field in ("token", "username", "password"):
            value = getattr(self, field)
            if value and not value.startswith("env://"):
                raise ValueError(f"auth {field} must be an env:// secret reference")
        return self


class HttpToolPagination(_Strict):
    type: Literal["none", "link_header", "page_param"] = "none"
    # page_param: the query parameter carrying the page number, counted from start_page.
    page_param: str = Field(default="page", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    start_page: int = Field(default=1, ge=0)
    max_pages: int = Field(default=5, ge=1, le=50)
    # The list to concatenate across pages; "" means the whole body is the list.
    items_pointer: str = ""


class HttpToolResponse(_Strict):
    # RFC 6901 JSON pointer into the body, e.g. "/data/items"; "" keeps the whole body.
    select: str = ""
    output_schema: dict[str, Any] | None = None


class HttpToolConfig(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]+$")
    description: str = Field(min_length=1, max_length=2000)
    version: str = "1.0.0"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str = Field(min_length=8, max_length=2048)
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )
    query: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    auth: HttpToolAuth = Field(default_factory=HttpToolAuth)
    pagination: HttpToolPagination = Field(default_factory=HttpToolPagination)
    response: HttpToolResponse = Field(default_factory=HttpToolResponse)
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    max_result_bytes: int = Field(default=262_144, ge=1024, le=4_194_304)
    side_effect: SideEffect | None = None
    idempotency: Idempotency | None = None
    required_permissions: frozenset[str] = frozenset()
    # Reads state that changes on its own (a job's status): never held back as a repeat.
    volatile: bool = False

    @model_validator(mode="after")
    def consistent(self) -> HttpToolConfig:
        parts = urlsplit(self.url)
        if parts.scheme not in {"https", "http"} or not parts.hostname:
            raise ValueError(f"http tool {self.name!r} needs an absolute http(s) URL")
        if _PLACEHOLDER.search(parts.netloc):
            raise ValueError(f"http tool {self.name!r} cannot template the host")
        if self.parameters.get("type") != "object":
            raise ValueError(f"http tool {self.name!r} parameters must be a JSON object schema")
        known = set(self.parameters.get("properties", {}))
        required = set(self.parameters.get("required", ()))
        for placeholder in _PLACEHOLDER.findall(parts.path):
            if placeholder not in required:
                raise ValueError(
                    f"http tool {self.name!r} uses {{{placeholder}}} in its path, which must be "
                    "a required parameter"
                )
        for placeholder in _placeholders([self.query, self.headers, self.body, parts.query]):
            if placeholder not in known:
                raise ValueError(
                    f"http tool {self.name!r} uses {{{placeholder}}}, which is not a parameter"
                )
        if self.body is not None and self.method == "GET":
            raise ValueError(f"http tool {self.name!r} cannot send a body with GET")
        refused = {"authorization", "cookie", "host"}
        if self.auth.type in {"caller", "header"} and self.auth.header_name:
            # A template must not write into the header the API authenticates with.
            refused.add(self.auth.header_name.lower())
        for header in self.headers:
            if header.lower() in refused:
                raise ValueError(
                    f"http tool {self.name!r} sets {header!r}; use auth for credentials"
                )
        for field, pointer in (
            ("response.select", self.response.select),
            ("pagination.items_pointer", self.pagination.items_pointer),
        ):
            if pointer and not pointer.startswith("/"):
                raise ValueError(f"http tool {self.name!r} {field} must be a JSON pointer")
        return self

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or "").lower()


def http_api_tool(
    config: HttpToolConfig,
    *,
    allowed_hosts: tuple[str, ...],
    allow_private_networks: bool = False,
    secrets: SecretResolver,
    client: httpx.AsyncClient | None = None,
) -> Tool:
    """The agent tool for one declared HTTP operation."""
    http_client = client or create_safe_http_client(
        allowed_hosts=allowed_hosts,
        allow_private_networks=allow_private_networks,
        timeout=config.timeout_seconds,
        follow_redirects=False,
    )

    caller_auth = config.auth.type == "caller"
    origin = _origin(config.url)

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url, url_query = _render_url(config.name, config.url, arguments)
        # The URL's own query first, then the query templates.
        query = [*url_query, *_render_mapping(config.query, arguments).items()]
        headers = {"Accept": "application/json", **_render_mapping(config.headers, arguments)}
        if caller_auth:
            headers.update(_caller_headers(config.name, config.auth, context))
        else:
            headers.update(await _auth_headers(config.auth, secrets))
        body = _render_value(config.body, arguments) if config.body is not None else None
        collected: list[Any] = []
        data: Any = None
        pages = 0
        total_bytes = 0
        status = 0
        response_headers: dict[str, str] = {}
        page_number = config.pagination.start_page
        next_url: str | None = url
        while next_url is not None and pages < config.pagination.max_pages:
            page_query = list(query)
            if config.pagination.type == "page_param":
                page_param = config.pagination.page_param
                page_query = [pair for pair in page_query if pair[0] != page_param]
                page_query.append((page_param, str(page_number)))
            await validate_outbound_url(next_url, allowed_hosts, allow_private_networks)
            # A Link header's next URL already carries its own query.
            following_link = config.pagination.type == "link_header" and next_url != url
            if caller_auth and following_link and _origin(next_url) != origin:
                # The user's credential goes only to the declared origin, never to another host
                # a response points at, even an allowlisted one.
                raise ToolExecutionError(
                    f"{config.name}: the next page is on another origin; the caller's credential "
                    "is not sent there"
                )
            response = await http_client.request(
                config.method,
                next_url,
                params=None if following_link else tuple(page_query),
                headers=headers,
                json=body,
                timeout=config.timeout_seconds,
            )
            pages += 1
            status = response.status_code
            response_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() in _KEPT_HEADERS
            }
            total_bytes += len(response.content)
            if total_bytes > config.max_result_bytes:
                raise ToolExecutionError(
                    f"the response from {config.name} is larger than {config.max_result_bytes} bytes"
                )
            parsed = _parse_body(response)
            if not response.is_success:
                return {
                    "ok": False,
                    "status": status,
                    "headers": response_headers,
                    "data": parsed,
                    "pages": pages,
                }
            if config.pagination.type == "none":
                data = parsed
                break
            items = _pointer(parsed, config.pagination.items_pointer)
            if not isinstance(items, list):
                raise ToolExecutionError(
                    f"{config.name}: pagination.items_pointer did not point at a list"
                )
            collected.extend(items)
            if config.pagination.type == "link_header":
                found = _LINK_NEXT.search(response.headers.get("link", ""))
                next_url = found.group(1) if found else None
            else:
                page_number += 1
                next_url = url if items else None
        if config.pagination.type != "none":
            data = collected
        selected = (
            _pointer(data, config.response.select)
            if config.response.select and config.pagination.type == "none"
            else data
        )
        return {
            "ok": True,
            "status": status,
            "headers": response_headers,
            "data": selected,
            "pages": pages,
        }

    side_effect = config.side_effect or (
        SideEffect.READ if config.method == "GET" else SideEffect.EXTERNAL
    )
    idempotency = config.idempotency or (
        Idempotency.IDEMPOTENT if config.method in {"GET", "PUT", "DELETE"} else Idempotency.KEYED
    )
    output_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "status": {"type": "integer"},
            "headers": {"type": "object"},
            "pages": {"type": "integer"},
            "data": config.response.output_schema or {},
        },
        "required": ["ok", "status", "data"],
    }
    return Tool(
        ToolDefinition(
            name=config.name,
            version=config.version,
            description=config.description,
            input_schema=config.parameters,
            output_schema=output_schema,
            required_permissions=config.required_permissions,
            side_effect=side_effect,
            idempotency=idempotency,
            timeout_seconds=config.timeout_seconds + 5,
            max_result_bytes=config.max_result_bytes + 4096,
            volatile=config.volatile,
        ),
        execute,
    )


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    return parts.scheme, (parts.hostname or "").lower(), port


def _caller_headers(name: str, auth: HttpToolAuth, context: ToolContext) -> dict[str, str]:
    """The header carrying the caller's credential; never echoes the credential in errors.

    Every refusal comes before a request is sent, so it is a :class:`ToolNotAttemptedError`.
    """
    # Only spaces and tabs are trimmed: a line break anywhere is refused below, not dropped.
    value = (context.credentials.get(auth.credential) or "").strip(_BLANKS)
    if auth.scheme:
        head = value.replace("\t", " ").partition(" ")[0]
        if head.lower() == auth.scheme.lower():
            value = value[len(head) :].strip(_BLANKS)
    if not value:
        raise ToolNotAttemptedError(
            f"{name}: authentication required: the caller's {auth.credential!r} credential was "
            "not passed for this run; sign in again and retry"
        )
    if auth.scheme:
        if any(blank in value for blank in _BLANKS):
            # Another scheme ("Basic ...") or a second word: not one token of this scheme.
            raise ToolNotAttemptedError(
                f"{name}: the caller's credential is not a {auth.scheme} credential"
            )
        value = f"{auth.scheme} {value}"
    if not _HEADER_VALUE.fullmatch(value):
        raise ToolNotAttemptedError(f"{name}: the caller's credential is not a valid header value")
    return {auth.header_name or "Authorization": value}


async def _auth_headers(auth: HttpToolAuth, secrets: SecretResolver) -> dict[str, str]:
    if auth.type == "bearer":
        return {"Authorization": f"Bearer {await secrets(auth.token or '')}"}
    if auth.type == "basic":
        pair = f"{await secrets(auth.username or '')}:{await secrets(auth.password or '')}"
        return {"Authorization": "Basic " + base64.b64encode(pair.encode()).decode()}
    if auth.type == "header":
        return {auth.header_name or "": await secrets(auth.token or "")}
    return {}


def _render_url(
    name: str, template: str, arguments: Mapping[str, Any]
) -> tuple[str, list[tuple[str, str]]]:
    """The URL without its query, and the template's query entries, filled in.

    Each path value is a model argument escaped as exactly one segment: one that is empty, ``.``
    or ``..`` (also percent-encoded) would point the call at another path, so it is refused.
    """
    parts = urlsplit(template)

    def segment(match: re.Match[str]) -> str:
        value = str(arguments[match.group(1)])
        if unquote(value).strip() in _UNSAFE_SEGMENTS:
            raise ToolNotAttemptedError(
                f"{name}: {match.group(1)!r} must name one path segment; it cannot be empty, "
                "'.' or '..'"
            )
        return quote(value, safe="")

    path = _PLACEHOLDER.sub(segment, parts.path)
    query = [
        (key, rendered)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if (rendered := _render_text(value, arguments)) is not None
    ]
    return urlunsplit((parts.scheme, parts.netloc, path, "", "")), query


def _render_mapping(templates: Mapping[str, str], arguments: Mapping[str, Any]) -> dict[str, str]:
    """Each entry with its placeholders filled; an entry naming a missing argument is left out."""
    rendered: dict[str, str] = {}
    for key, template in templates.items():
        value = _render_text(template, arguments)
        if value is not None:
            rendered[key] = value
    return rendered


def _render_text(template: str, arguments: Mapping[str, Any]) -> str | None:
    names = _PLACEHOLDER.findall(template)
    if any(arguments.get(name) is None for name in names):
        return None
    return _PLACEHOLDER.sub(lambda m: _as_text(arguments[m.group(1)]), template)


def _render_value(template: Any, arguments: Mapping[str, Any]) -> Any:
    if isinstance(template, dict):
        rendered = {key: _render_value(item, arguments) for key, item in template.items()}
        return {key: value for key, value in rendered.items() if value is not _MISSING}
    if isinstance(template, list):
        return [
            item
            for item in (_render_value(value, arguments) for value in template)
            if item is not _MISSING
        ]
    if isinstance(template, str):
        whole = _WHOLE_PLACEHOLDER.match(template)
        if whole:
            # "{param}" alone keeps the argument's JSON type.
            return arguments.get(whole.group(1), _MISSING)
        text = _render_text(template, arguments)
        return _MISSING if text is None else text
    return template


_MISSING: Any = object()


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _parse_body(response: httpx.Response) -> Any:
    if "json" in response.headers.get("content-type", "") and response.content:
        try:
            return response.json()
        except ValueError:
            pass
    return response.text


def _pointer(document: Any, pointer: str) -> Any:
    if not pointer:
        return document
    current = document
    for raw in pointer.lstrip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            return None
    return current


def _placeholders(values: list[Any]) -> set[str]:
    found: set[str] = set()
    for value in values:
        if isinstance(value, str):
            found.update(_PLACEHOLDER.findall(value))
        elif isinstance(value, dict):
            found.update(_placeholders(list(value.values())))
        elif isinstance(value, list):
            found.update(_placeholders(value))
    return found
