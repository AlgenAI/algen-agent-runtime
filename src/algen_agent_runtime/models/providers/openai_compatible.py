from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any, cast

import httpx

from algen_agent_runtime.config.settings import EnvironmentSecretProvider
from algen_agent_runtime.exceptions.errors import InvalidToolCallError, ProviderError
from algen_agent_runtime.runtime.attachments import describe_for_text
from algen_agent_runtime.security.redaction import REDACTED, is_sensitive_key, redact
from algen_agent_runtime.types.contracts import (
    ErrorKind,
    FileBlock,
    FinishReason,
    Message,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    Role,
    TokenUsage,
    ToolCall,
)


def _error_kind(status: int) -> tuple[ErrorKind, bool]:
    if status == 401:
        return ErrorKind.AUTHENTICATION, False
    if status == 403:
        return ErrorKind.AUTHORIZATION, False
    if status == 408:
        return ErrorKind.TIMEOUT, True
    if status == 429:
        return ErrorKind.RATE_LIMIT, True
    if status >= 500:
        return ErrorKind.UNAVAILABLE, True
    return ErrorKind.INVALID_REQUEST, False


def _sent_credentials(response: httpx.Response) -> list[str]:
    """The credentials the request carried (its credential-named headers' values, and the token
    after a scheme such as ``Bearer``), longest first."""
    try:
        headers = response.request.headers
    except RuntimeError:
        return []
    held: set[str] = set()
    for key, value in headers.items():
        if not value or not is_sensitive_key(key):
            continue
        held.add(value)
        parts = value.split(None, 1)
        if len(parts) == 2:
            held.add(parts[1].strip())
    return sorted((item for item in held if len(item) >= 6), key=len, reverse=True)


def _error_payload(payload: Any) -> Mapping[str, Any] | None:
    """The error object of an error body: ``{"error": {...}}`` or a bare ``{...}``, either one
    also wrapped in a one-item list (Gemini's OpenAI-compatible endpoint answers
    ``[{"error": ...}]``)."""
    if isinstance(payload, list) and len(payload) == 1:
        payload = payload[0]
    if not isinstance(payload, Mapping):
        return None
    error = payload.get("error")
    return error if isinstance(error, Mapping) else payload


def _error_detail(source: Mapping[str, Any] | None, credentials: Sequence[str]) -> str:
    """An error object's message, type, code, status and param, bounded, with the request's
    credentials and anything secret-shaped masked."""
    parts: list[str] = []
    for key in ("message", "type", "code", "status", "param"):
        value = source.get(key) if source is not None else None
        if value is not None and not isinstance(value, (Mapping, list)) and str(value).strip():
            label = "" if key == "message" else f"{key}="
            parts.append(f"{label}{value}")
    detail = "; ".join(parts)
    if not detail:
        return ""
    for credential in credentials:
        detail = detail.replace(credential, REDACTED)
    detail = " ".join(detail.split())[:500]
    return str(redact(detail))


def _error_message(response: httpx.Response, provider_id: str) -> str:
    """Return bounded provider diagnostics without retaining the raw response body.

    A provider that quotes the API key it was sent ("Incorrect API key provided: ...") has the
    key masked (0.2.0b5), whatever its shape."""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = _error_detail(_error_payload(payload), _sent_credentials(response))
    if detail:
        return f"{provider_id} returned HTTP {response.status_code}: {detail}"
    return f"{provider_id} returned HTTP {response.status_code}"


# Words in an error object's ``type``, ``code`` or ``status`` that say what failed, for an error
# a stream reports in its body after HTTP 200, where there is no status to go by.
_STREAM_ERROR_WORDS: tuple[tuple[tuple[str, ...], ErrorKind, bool], ...] = (
    (("rate_limit", "too_many_requests", "resource_exhausted"), ErrorKind.RATE_LIMIT, True),
    (("timeout", "deadline"), ErrorKind.TIMEOUT, True),
    (("authentication", "invalid_api_key", "unauthenticated"), ErrorKind.AUTHENTICATION, False),
    (("permission", "forbidden"), ErrorKind.AUTHORIZATION, False),
    (
        ("invalid_request", "invalid_argument", "not_found", "context_length"),
        ErrorKind.INVALID_REQUEST,
        False,
    ),
)


def stream_error_kind(error: Mapping[str, Any]) -> tuple[ErrorKind, bool]:
    """The kind of an error a stream reported in its body (an OpenAI-style ``{"error": ...}``
    chunk), and whether retrying may help.

    A numeric ``code`` is read as the HTTP status it names; otherwise words in ``type``, ``code``
    and ``status`` decide. Anything else, such as ``server_error`` or an overloaded model, is a
    retryable ``unavailable``: the provider broke the answer off, not the request.
    """
    code = error.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 600:
        return _error_kind(code)
    words = f"{error.get('type') or ''} {code or ''} {error.get('status') or ''}".lower()
    for markers, kind, retryable in _STREAM_ERROR_WORDS:
        if any(marker in words for marker in markers):
            return kind, retryable
    return ErrorKind.UNAVAILABLE, True


def stream_error(
    provider_id: str, error: Any, response: httpx.Response, kind: ErrorKind, retryable: bool
) -> ProviderError:
    """A ``ProviderError`` for an error a stream reported after HTTP 200, so an answer the
    provider broke off is never reported complete. Its text is bounded and masked like an HTTP
    error's; ``status_code`` is None, since the HTTP status was 200."""
    source = error if isinstance(error, Mapping) else {"message": error}
    detail = _error_detail(source, _sent_credentials(response))
    message = f"{provider_id} stream failed" + (f": {detail}" if detail else "")
    return ProviderError(message, kind, retryable)


def _token_usage(usage: Mapping[str, Any]) -> TokenUsage:
    """An OpenAI-style ``usage`` object, plain or streamed, with the prompt tokens read from the
    cache (``prompt_tokens_details.cached_tokens``) as ``cached_tokens``."""
    details = usage.get("prompt_tokens_details") or {}
    cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
    return TokenUsage(
        input_tokens=usage.get("prompt_tokens") or 0,
        output_tokens=usage.get("completion_tokens") or 0,
        cached_tokens=cached or 0,
    )


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    raise TypeError("assistant message content must be text or content blocks")


def tool_arguments(
    value: Any, provider: str, tool: str, *, cut_off: bool = False
) -> dict[str, Any]:
    """A tool call's arguments as a dict; ``InvalidToolCallError`` when they are not an object.

    ``cut_off`` says the response stopped at the output-token limit, so a call that does not
    decode was cut short rather than written wrong.
    """
    if value is None or value == "":
        return {}
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        # The decoder's message gives the position only, never the text.
        raise InvalidToolCallError(provider, tool, str(exc), cut_off=cut_off) from None
    if not isinstance(decoded, Mapping):
        raise InvalidToolCallError(
            provider, tool, "arguments are not a JSON object", cut_off=cut_off
        )
    return dict(decoded)


def _bounds(low: Any, high: Any, unit: str = "") -> str | None:
    suffix = f" {unit}" if unit else ""
    if low is not None and high is not None:
        return f"Between {low} and {high}{suffix}."
    if low is not None:
        return f"At least {low}{suffix}."
    if high is not None:
        return f"At most {high}{suffix}."
    return None


def constraint_note(node: Mapping[str, Any], dropped: Collection[str]) -> str | None:
    """The constraints in ``node`` that a provider's schema subset drops, in words.

    A provider that rejects ``maxItems`` or ``pattern`` never shows them to the model. Put into
    the node's ``description``, they still guide the first answer, and the Runtime's verifier
    still enforces the full schema. Only scalar keyword values count, so a property that happens
    to be named ``pattern`` is not mistaken for one.
    """

    def value(key: str) -> Any:
        item = node.get(key) if key in dropped else None
        return item if isinstance(item, (int, float, str)) and not isinstance(item, bool) else None

    notes = [
        _bounds(value("minItems"), value("maxItems"), "items"),
        _bounds(value("minLength"), value("maxLength"), "characters"),
        _bounds(value("minProperties"), value("maxProperties"), "properties"),
        _bounds(value("minimum"), value("maximum")),
    ]
    if (above := value("exclusiveMinimum")) is not None:
        notes.append(f"Greater than {above}.")
    if (below := value("exclusiveMaximum")) is not None:
        notes.append(f"Less than {below}.")
    if (step := value("multipleOf")) is not None:
        notes.append(f"A multiple of {step}.")
    if (pattern := value("pattern")) is not None:
        notes.append(f"Must match the pattern {pattern}.")
    if "uniqueItems" in dropped and node.get("uniqueItems") is True:
        notes.append("Items must be unique.")
    if (form := value("format")) is not None:
        notes.append(f"Format: {form}.")
    return " ".join(note for note in notes if note) or None


def with_constraint_note(
    result: dict[str, Any], node: Mapping[str, Any], dropped: Collection[str]
) -> dict[str, Any]:
    """``result`` with ``node``'s dropped constraints added to its description."""
    note = constraint_note(node, dropped)
    if note:
        description = node.get("description")
        prefix = f"{description} " if isinstance(description, str) and description else ""
        result["description"] = prefix + note
    return result


def openai_strict_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Translate ordinary JSON Schema into OpenAI's strict structured-output subset.

    OpenAI requires every object property to be listed in ``required``, including fields that
    application models normally give defaults. Nullable fields remain nullable through their
    existing ``anyOf``/``type`` declaration, but must still be emitted by the model.
    """
    normalized = deepcopy(dict(schema))
    unsupported_constraints = {
        "default",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "uniqueItems",
    }

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            # A map of names to schemas: its keys are names, never keywords to drop.
            key: {name: visit(item[name]) for name in item}
            if key in ("properties", "$defs", "definitions") and isinstance(item, dict)
            else visit(item)
            for key, item in value.items()
            if key not in unsupported_constraints
        }
        with_constraint_note(result, value, unsupported_constraints)
        if "const" in result:
            result["enum"] = [result.pop("const")]
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result

    return cast(dict[str, Any], visit(normalized))


# A wire alias's hash suffix: an underscore and twelve hexadecimal digits.
_HASHED = re.compile(r"_[0-9a-f]{12}$")


def _fits(name: str) -> bool:
    """Whether a provider accepts ``name`` as a function name."""
    return len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None


class OpenAICompatibleProvider:
    # Ask for token usage on streamed calls; servers that reject ``stream_options`` turn it off
    # with the provider setting ``stream_usage: false``.
    default_stream_usage: bool = True
    # How long an ``embed`` call may take; a slower one fails with a retryable ``timeout``.
    embed_timeout_seconds: float = 60.0

    def __init__(
        self,
        provider_id: str,
        base_url: str,
        api_key_reference: str | None,
        default_model: str,
        capabilities: ModelCapabilities,
        secret_provider: Any | None = None,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
        endpoint: str = "/chat/completions",
        stream_usage: bool | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._key_reference = api_key_reference
        self._default_model = default_model
        self._capabilities = capabilities
        self._secrets = secret_provider or EnvironmentSecretProvider()
        self._client = client or httpx.AsyncClient()
        self._headers = headers or {}
        self._endpoint = endpoint
        self._stream_usage = self.default_stream_usage if stream_usage is None else stream_usage
        self._wire_tool_names: dict[str, str] = {}
        self._runtime_tool_names: dict[str, str] = {}

    @property
    def provider_id(self) -> str:
        return self._provider_id

    async def capabilities(self, model: str) -> ModelCapabilities:
        return self._capabilities

    def _http_error(self, response: httpx.Response) -> ProviderError:
        """The ``ProviderError`` for an HTTP error response whose body has been read: its kind
        from the status (see ``_error_kind_for``), its text bounded and masked."""
        kind, retryable = self._error_kind_for(response)
        return ProviderError(
            _error_message(response, self.provider_id),
            kind,
            retryable,
            status_code=response.status_code,
        )

    def _error_kind_for(self, response: httpx.Response) -> tuple[ErrorKind, bool]:
        """The kind of an HTTP error response, and whether retrying may help. A provider whose
        statuses mean something else (Gemini's 400 for a wrong key) overrides it."""
        return _error_kind(response.status_code)

    async def _auth_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._key_reference:
            headers["Authorization"] = f"Bearer {await self._secrets.get(self._key_reference)}"
        return headers

    def _wire_tool_name(self, name: str) -> str:
        """Map namespaced Runtime names to the common provider wire format.

        Runtime tool names intentionally support namespaces such as
        ``teaching.build_study_plan``. OpenAI-compatible APIs and Anthropic limit
        function names to 64 ASCII letters, digits, underscores, and hyphens.
        A name that fits is sent as it is. A dotted name is sent readable when it
        can be (``teaching__build_study_plan``): models copy a readable name
        exactly, where they shorten one with a hash suffix. Only when the readable
        form does not fit or is taken is a hash suffix added. Either way the
        instance maps responses back to the stable Runtime name.
        """
        if cached := self._wire_tool_names.get(name):
            return cached
        readable = name.replace(".", "__")
        if _fits(name) and self._runtime_tool_names.get(name, name) == name:
            alias = name
        elif _fits(readable) and readable not in self._runtime_tool_names:
            alias = readable
        else:
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_-") or "tool"
            digest = sha256(name.encode("utf-8")).hexdigest()[:12]
            alias = f"{stem[:51]}_{digest}"
        existing = self._runtime_tool_names.get(alias)
        if existing is not None and existing != name:
            raise ValueError("tool name alias collision")
        self._wire_tool_names[name] = alias
        self._runtime_tool_names[alias] = name
        return alias

    def _runtime_tool_name(self, name: str) -> str:
        """The Runtime name for a tool name a model returned.

        A model sometimes drops an alias's hash suffix (``hr_find_people`` for
        ``hr_find_people_0a1b2c3d4e5f``) or writes the Runtime name itself. Such a
        name maps back when exactly one tool matches it; any other name is returned
        as it is, for the Runtime to report as unknown.
        """
        if name in self._runtime_tool_names:
            return self._runtime_tool_names[name]
        matches = {
            runtime
            for alias, runtime in self._runtime_tool_names.items()
            if runtime == name or _HASHED.sub("", alias) == name
        }
        return matches.pop() if len(matches) == 1 else name

    def _file_part(self, block: FileBlock) -> dict[str, Any]:
        """An attached file as a content part: images as data URLs, PDFs as ``file`` parts
        where the API takes them (``files`` capability), anything else as a short note."""
        if block.content is None:
            return {"type": "text", "text": describe_for_text(block)}
        data = base64.b64encode(block.content).decode("ascii")
        url = f"data:{block.media_type};base64,{data}"
        if (block.media_type or "").startswith("image/"):
            return {"type": "image_url", "image_url": {"url": url}}
        if block.media_type == "application/pdf" and self._capabilities.files:
            return {
                "type": "file",
                "file": {"filename": block.name or f"{block.artifact_id}.pdf", "file_data": url},
            }
        return {"type": "text", "text": describe_for_text(block)}

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        # A tool message takes text only, so files a tool returned follow the run of tool
        # results they belong to, in one user message.
        held: dict[str, list[dict[str, Any]]] = {}

        def release() -> None:
            if held:
                parts: list[dict[str, Any]] = []
                for call_id, files in held.items():
                    parts.append(
                        {"type": "text", "text": f"Files returned by tool call {call_id}:"}
                    )
                    parts.extend(files)
                messages.append({"role": "user", "content": parts})
                held.clear()

        for message in request.messages:
            if message.role != Role.TOOL:
                release()
            content: Any = message.text_content
            blocks: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    blocks.append({"type": "text", "text": block.text})
                elif block.type == "image":
                    url = block.url or f"data:{block.media_type};base64,{block.data}"
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
                elif block.type == "file":
                    part = self._file_part(block)
                    if message.role == Role.TOOL and part["type"] != "text":
                        held.setdefault(message.tool_call_id or "", []).append(part)
                        part = {"type": "text", "text": f"{describe_for_text(block)} (below)"}
                    blocks.append(part)
            if len(blocks) > 1 or any(block["type"] != "text" for block in blocks):
                content = blocks
            item: dict[str, Any] = {"role": message.role.value, "content": content}
            if message.name:
                item["name"] = (
                    self._wire_tool_name(message.name)
                    if message.role == Role.TOOL
                    else message.name
                )
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": self._wire_tool_name(call.name),
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in message.tool_calls
                ]
            messages.append(item)
        release()
        payload: dict[str, Any] = {
            "model": request.model or self._default_model,
            "messages": messages,
            "stream": request.stream,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": self._wire_tool_name(tool.name),
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in request.tools
            ]
        if request.response_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }
        payload.update(request.extensions)
        return payload

    def _response(self, data: dict[str, Any], started: float, raw: bool) -> ModelResponse:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            cut_off = choice.get("finish_reason") == "length"
            calls = tuple(
                ToolCall(
                    id=item.get("id") or f"call-{index}",
                    name=(name := self._runtime_tool_name(item["function"]["name"])),
                    arguments=tool_arguments(
                        item["function"].get("arguments"), self.provider_id, name, cut_off=cut_off
                    ),
                )
                for index, item in enumerate(message.get("tool_calls") or ())
            )
            text = _message_text(message.get("content"))
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                f"{self.provider_id} returned an invalid chat response: {exc}",
                ErrorKind.INVALID_RESPONSE,
                True,
            ) from exc
        finish_map = {
            "stop": FinishReason.STOP,
            "tool_calls": FinishReason.TOOL_CALLS,
            "length": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
        }
        return ModelResponse(
            id=data.get("id", "unknown"),
            message=Message.text(Role.ASSISTANT, text),
            tool_calls=calls,
            finish_reason=finish_map.get(choice.get("finish_reason"), FinishReason.STOP),
            usage=_token_usage(data.get("usage") or {}),
            latency_ms=(time.monotonic() - started) * 1000,
            model=data.get("model", self._default_model),
            provider=self.provider_id,
            raw_metadata=data if raw else None,
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        try:
            response = await self._client.post(
                self._base_url + self._endpoint,
                headers=await self._auth_headers(),
                json=self._payload(request.model_copy(update={"stream": False})),
                timeout=request.timeout_seconds,
            )
            if response.is_error:
                raise self._http_error(response)
            return self._response(response.json(), started, request.raw_response_enabled)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.provider_id} timed out", ErrorKind.TIMEOUT, True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.provider_id} transport failure", ErrorKind.UNAVAILABLE, True
            ) from exc

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        response_id = "unknown"
        model = request.model or self._default_model
        text_parts: list[str] = []
        tool_parts: dict[int, dict[str, Any]] = {}
        finish_reason = FinishReason.STOP
        usage = TokenUsage()
        payload = self._payload(request.model_copy(update={"stream": True}))
        if self._stream_usage:
            # Without this OpenAI reports no usage for a stream, so budgets and metering see 0.
            options = payload.get("stream_options")
            payload["stream_options"] = {
                "include_usage": True,
                **(options if isinstance(options, Mapping) else {}),
            }
        try:
            async with self._client.stream(
                "POST",
                self._base_url + self._endpoint,
                headers=await self._auth_headers(),
                json=payload,
                timeout=request.timeout_seconds,
            ) as response:
                if response.is_error:
                    await response.aread()
                    raise self._http_error(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ")
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    if isinstance(chunk, Mapping) and chunk.get("error") is not None:
                        # The provider broke the answer off (overloaded, a server error): the
                        # text so far is not the answer, so the stream fails rather than ends.
                        error = chunk["error"]
                        kind, retryable = stream_error_kind(
                            error if isinstance(error, Mapping) else {}
                        )
                        raise stream_error(self.provider_id, error, response, kind, retryable)
                    response_id = chunk.get("id", response_id)
                    model = chunk.get("model", model)
                    chunk_usage = chunk.get("usage") or {}
                    if chunk_usage:
                        usage = _token_usage(chunk_usage)
                    # The usage chunk that ends a stream carries an empty ``choices`` list.
                    choices = chunk.get("choices") or [{}]
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if text := delta.get("content"):
                        text_parts.append(text)
                        yield ModelStreamEvent(type="delta", delta=text)
                    for item in delta.get("tool_calls", ()):
                        index = int(item.get("index", 0))
                        current = tool_parts.setdefault(
                            index, {"id": None, "name": "", "arguments": ""}
                        )
                        if item.get("id"):
                            current["id"] = item["id"]
                        function = item.get("function") or {}
                        current["name"] += function.get("name") or ""
                        arguments = function.get("arguments")
                        if isinstance(arguments, Mapping):
                            current["arguments"] = dict(arguments)
                        elif arguments:
                            current["arguments"] += str(arguments)
                    finish_map = {
                        "stop": FinishReason.STOP,
                        "tool_calls": FinishReason.TOOL_CALLS,
                        "length": FinishReason.LENGTH,
                        "content_filter": FinishReason.CONTENT_FILTER,
                    }
                    if choice.get("finish_reason"):
                        finish_reason = finish_map.get(choice["finish_reason"], FinishReason.STOP)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.provider_id} stream timed out", ErrorKind.TIMEOUT, True
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.provider_id} stream transport failure", ErrorKind.UNAVAILABLE, True
            ) from exc
        calls = tuple(
            ToolCall(
                id=item["id"] or f"call-{index}",
                name=(name := self._runtime_tool_name(item["name"])),
                arguments=tool_arguments(
                    item["arguments"],
                    self.provider_id,
                    name,
                    cut_off=finish_reason == FinishReason.LENGTH,
                ),
            )
            for index, item in sorted(tool_parts.items())
        )
        for call in calls:
            yield ModelStreamEvent(type="tool_call", tool_call=call)
        yield ModelStreamEvent(
            type="completed",
            response=ModelResponse(
                id=response_id,
                message=Message.text(Role.ASSISTANT, "".join(text_parts)),
                tool_calls=calls,
                finish_reason=FinishReason.TOOL_CALLS if calls else finish_reason,
                usage=usage,
                latency_ms=(time.monotonic() - started) * 1000,
                model=model,
                provider=self.provider_id,
            ),
        )

    def _embeddings_url(self, model: str | None) -> str:
        """Where ``embed`` posts for ``model``."""
        return self._base_url + "/embeddings"

    def _embeddings_payload(self, texts: Sequence[str], model: str | None) -> dict[str, Any]:
        return {"model": model or self._default_model, "input": list(texts)}

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        """One vector per text. Fails with a ``ProviderError`` like a chat call: an HTTP error by
        its status, a call slower than ``embed_timeout_seconds`` as a retryable ``timeout``, a
        refused or dropped connection as a retryable ``unavailable``."""
        try:
            response = await self._client.post(
                self._embeddings_url(model),
                headers=await self._auth_headers(),
                json=self._embeddings_payload(texts, model),
                timeout=self.embed_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{self.provider_id} embeddings timed out", ErrorKind.TIMEOUT, True
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{self.provider_id} embeddings transport failure", ErrorKind.UNAVAILABLE, True
            ) from exc
        if response.is_error:
            raise self._http_error(response)
        try:
            data = response.json()["data"]
            return [[float(value) for value in item["embedding"]] for item in data]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(
                f"{self.provider_id} returned an invalid embeddings response",
                ErrorKind.INVALID_RESPONSE,
                True,
            ) from exc

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                self._base_url + "/models", headers=await self._auth_headers(), timeout=5
            )
            return response.status_code < 500
        except httpx.HTTPError:
            return False
