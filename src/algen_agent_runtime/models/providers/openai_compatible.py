from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from hashlib import sha256
from typing import Any

import httpx

from algen_agent_runtime.config.settings import EnvironmentSecretProvider
from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.security.redaction import redact
from algen_agent_runtime.types.contracts import (
    ErrorKind,
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
    if status == 429:
        return ErrorKind.RATE_LIMIT, True
    if status >= 500:
        return ErrorKind.UNAVAILABLE, True
    return ErrorKind.INVALID_REQUEST, False


def _error_message(response: httpx.Response, provider_id: str) -> str:
    """Return bounded provider diagnostics without retaining the raw response body."""
    parts: list[str] = []
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        source = error if isinstance(error, dict) else payload
        for key in ("message", "type", "code", "param"):
            value = source.get(key)
            if value is not None and str(value).strip():
                label = "" if key == "message" else f"{key}="
                parts.append(f"{label}{value}")
    detail = "; ".join(parts)
    if detail:
        detail = " ".join(detail.split())[:500]
        detail = str(redact(detail))
        return f"{provider_id} returned HTTP {response.status_code}: {detail}"
    return f"{provider_id} returned HTTP {response.status_code}"


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


def _tool_arguments(value: Any) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, Mapping):
        raise TypeError("tool arguments must decode to a JSON object")
    return dict(decoded)


def openai_strict_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Translate ordinary JSON Schema into OpenAI's strict structured-output subset.

    OpenAI requires every object property to be listed in ``required``, including fields that
    application models normally give defaults. Nullable fields remain nullable through their
    existing ``anyOf``/``type`` declaration, but must still be emitted by the model.
    """
    normalized = deepcopy(dict(schema))

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(item) for key, item in value.items() if key != "default"}
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["properties"] = {key: visit(item) for key, item in properties.items()}
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result

    return visit(normalized)


class OpenAICompatibleProvider:
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
        self._wire_tool_names: dict[str, str] = {}
        self._runtime_tool_names: dict[str, str] = {}

    @property
    def provider_id(self) -> str:
        return self._provider_id

    async def capabilities(self, model: str) -> ModelCapabilities:
        return self._capabilities

    async def _auth_headers(self) -> dict[str, str]:
        headers = dict(self._headers)
        if self._key_reference:
            headers["Authorization"] = f"Bearer {await self._secrets.get(self._key_reference)}"
        return headers

    def _wire_tool_name(self, name: str) -> str:
        """Map namespaced Runtime names to the common provider wire format.

        Runtime tool names intentionally support namespaces such as
        ``teaching.build_study_plan``. OpenAI-compatible APIs generally limit
        function names to 64 ASCII letters, digits, underscores, and hyphens.
        The hash suffix prevents collisions while the instance maps responses
        back to the stable Runtime name.
        """
        if cached := self._wire_tool_names.get(name):
            return cached
        if len(name) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", name):
            alias = name
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
        return self._runtime_tool_names.get(name, name)

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        messages = []
        for message in request.messages:
            content: Any = message.text_content
            blocks: list[dict[str, Any]] = []
            for block in message.content:
                if block.type == "text":
                    blocks.append({"type": "text", "text": block.text})
                elif block.type == "image":
                    url = block.url or f"data:{block.media_type};base64,{block.data}"
                    blocks.append({"type": "image_url", "image_url": {"url": url}})
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
            calls = tuple(
                ToolCall(
                    id=item.get("id") or f"call-{index}",
                    name=self._runtime_tool_name(item["function"]["name"]),
                    arguments=_tool_arguments(item["function"].get("arguments")),
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
        usage = data.get("usage") or {}
        return ModelResponse(
            id=data.get("id", "unknown"),
            message=Message.text(Role.ASSISTANT, text),
            tool_calls=calls,
            finish_reason=finish_map.get(choice.get("finish_reason"), FinishReason.STOP),
            usage=TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
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
                kind, retryable = _error_kind(response.status_code)
                raise ProviderError(_error_message(response, self.provider_id), kind, retryable)
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
        try:
            async with self._client.stream(
                "POST",
                self._base_url + self._endpoint,
                headers=await self._auth_headers(),
                json=self._payload(request.model_copy(update={"stream": True})),
                timeout=request.timeout_seconds,
            ) as response:
                if response.is_error:
                    kind, retryable = _error_kind(response.status_code)
                    await response.aread()
                    raise ProviderError(_error_message(response, self.provider_id), kind, retryable)
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line.removeprefix("data: ")
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    response_id = chunk.get("id", response_id)
                    model = chunk.get("model", model)
                    chunk_usage = chunk.get("usage") or {}
                    if chunk_usage:
                        usage = TokenUsage(
                            input_tokens=chunk_usage.get("prompt_tokens", 0),
                            output_tokens=chunk_usage.get("completion_tokens", 0),
                            cached_tokens=(chunk_usage.get("prompt_tokens_details", {}) or {}).get(
                                "cached_tokens", 0
                            ),
                        )
                    choice = chunk.get("choices", [{}])[0]
                    delta = choice.get("delta", {})
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
                name=self._runtime_tool_name(item["name"]),
                arguments=_tool_arguments(item["arguments"]),
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

    async def embed(self, texts: Sequence[str], model: str | None = None) -> list[list[float]]:
        response = await self._client.post(
            self._base_url + "/embeddings",
            headers=await self._auth_headers(),
            json={"model": model or self._default_model, "input": list(texts)},
        )
        if response.is_error:
            kind, retryable = _error_kind(response.status_code)
            raise ProviderError(_error_message(response, self.provider_id), kind, retryable)
        return [item["embedding"] for item in response.json()["data"]]

    async def health(self) -> bool:
        try:
            response = await self._client.get(
                self._base_url + "/models", headers=await self._auth_headers(), timeout=5
            )
            return response.status_code < 500
        except httpx.HTTPError:
            return False
