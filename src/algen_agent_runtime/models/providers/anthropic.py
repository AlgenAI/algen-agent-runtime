"""Native Anthropic Messages API adapter.

Thinking blocks are read for nothing and never sent back. Current Claude models bind each
thinking block to the exact conversation that produced it, and the runtime's guardrails may
rewrite history between turns; replaying a block after such a rewrite is rejected, while
omitting it is always accepted. The model then reasons afresh on each turn.

Prompt caching is on by default. An agent loop re-sends the whole conversation on every call,
so without it each call pays full price for the tools, the instructions and every earlier turn.
The end of the system prompt carries an explicit cache breakpoint (tools and instructions are
read back on every call), and the request asks for automatic caching, which places a second
breakpoint on the last block and moves it forward as the conversation grows. Anthropic reports
``input_tokens`` as the uncached remainder only; usage here reports the whole prompt (uncached,
written to the cache and read from it) as ``input_tokens``, as other providers do, and the part
read from the cache as ``cached_tokens``. Pass ``prompt_caching=False`` for a gateway that
rejects the ``cache_control`` fields.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Mapping
from copy import deepcopy
from typing import Any

import httpx

from algen_agent_runtime.exceptions.errors import ProviderError
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
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

# JSON Schema keywords the structured-output endpoint rejects. The runtime's json_schema
# verifier still checks the full schema on the answer, so dropping them loses nothing.
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "default",
        "exclusiveMaximum",
        "exclusiveMinimum",
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
)
_SUPPORTED_STRING_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)
_CACHE = {"type": "ephemeral"}
_FINISH_REASONS = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "tool_use": FinishReason.TOOL_CALLS,
    "refusal": FinishReason.CONTENT_FILTER,
}


def anthropic_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Translate ordinary JSON Schema into the subset Anthropic structured outputs accept."""

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: visit(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYWORDS
            and not (key == "format" and item not in _SUPPORTED_STRING_FORMATS)
        }
        if result.get("type") == "object" or "properties" in result:
            result["additionalProperties"] = False
        return result

    translated: dict[str, Any] = visit(deepcopy(dict(schema)))
    return translated


def _usage(usage: Mapping[str, Any]) -> tuple[int, int]:
    """The whole prompt's input tokens, and the part of them read from the cache."""
    read = int(usage.get("cache_read_input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    return int(usage.get("input_tokens") or 0) + written + read, read


def _merge_extensions(payload: dict[str, Any], extensions: Mapping[str, Any]) -> None:
    # Profiles set e.g. ``output_config: {effort: low}``; keep the response format beside it.
    for key, value in extensions.items():
        current = payload.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            payload[key] = {**current, **value}
        else:
            payload[key] = value


class AnthropicProvider(OpenAICompatibleProvider):
    """Native Anthropic Messages API adapter with normalized contracts."""

    def __init__(
        self,
        api_key_reference: str,
        default_model: str,
        base_url: str = "https://api.anthropic.com/v1",
        *,
        prompt_caching: bool = True,
        **kwargs: Any,
    ) -> None:
        self._prompt_caching = prompt_caching
        super().__init__(
            "anthropic",
            base_url,
            api_key_reference,
            default_model,
            ModelCapabilities(
                streaming=True, tools=True, structured_output=True, json_schema=True, images=True
            ),
            endpoint="/messages",
            **kwargs,
        )

    async def _auth_headers(self) -> dict[str, str]:
        return {
            "x-api-key": await self._secrets.get(self._key_reference or ""),
            "anthropic-version": "2023-06-01",
        }

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        system = "\n".join(
            message.text_content for message in request.messages if message.role == Role.SYSTEM
        )
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == Role.SYSTEM:
                continue
            if message.role == Role.TOOL:
                result = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.text_content,
                }
                previous = messages[-1] if messages else None
                # Results of parallel calls belong in one user turn, not one turn each.
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and all(item.get("type") == "tool_result" for item in previous["content"])
                ):
                    previous["content"].append(result)
                    continue
                content: Any = [result]
                role = "user"
            else:
                role = message.role.value
                content = []
                for block in message.content:
                    if block.type == "text" and block.text:
                        content.append({"type": "text", "text": block.text})
                    elif block.type == "image":
                        if block.data:
                            content.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": block.media_type,
                                        "data": block.data,
                                    },
                                }
                            )
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": self._wire_tool_name(call.name),
                        "input": call.arguments,
                    }
                    for call in message.tool_calls
                )
            messages.append({"role": role, "content": content})
        payload: dict[str, Any] = {
            "model": request.model or self._default_model,
            "messages": messages,
            "max_tokens": request.max_output_tokens or 1024,
            "stream": request.stream,
        }
        if system and self._prompt_caching:
            payload["system"] = [{"type": "text", "text": system, "cache_control": dict(_CACHE)}]
        elif system:
            payload["system"] = system
        if self._prompt_caching:
            payload["cache_control"] = dict(_CACHE)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tools:
            payload["tools"] = [
                {
                    "name": self._wire_tool_name(tool.name),
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
                for tool in request.tools
            ]
        if request.response_schema:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": anthropic_json_schema(request.response_schema),
                }
            }
        _merge_extensions(payload, request.extensions)
        return payload

    def _response(self, data: dict[str, Any], started: float, raw: bool) -> ModelResponse:
        text = "".join(
            item.get("text", "") for item in data.get("content", ()) if item.get("type") == "text"
        )
        calls = tuple(
            ToolCall(
                id=item["id"],
                name=self._runtime_tool_name(item["name"]),
                arguments=item.get("input", {}),
            )
            for item in data.get("content", ())
            if item.get("type") == "tool_use"
        )
        input_tokens, cached_tokens = _usage(data.get("usage") or {})
        return ModelResponse(
            id=data.get("id", "unknown"),
            message=Message.text(Role.ASSISTANT, text),
            tool_calls=calls,
            finish_reason=FinishReason.TOOL_CALLS
            if calls
            else _FINISH_REASONS.get(str(data.get("stop_reason")), FinishReason.STOP),
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=(data.get("usage") or {}).get("output_tokens", 0),
                cached_tokens=cached_tokens,
            ),
            latency_ms=(time.monotonic() - started) * 1000,
            model=data.get("model", self._default_model),
            provider=self.provider_id,
            raw_metadata=data if raw else None,
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        response_id = "unknown"
        model = request.model or self._default_model
        text_parts: list[str] = []
        call_parts: dict[int, dict[str, Any]] = {}
        usage = TokenUsage()
        stop_reason = FinishReason.STOP
        try:
            async with self._client.stream(
                "POST",
                self._base_url + self._endpoint,
                headers=await self._auth_headers(),
                json=self._payload(request.model_copy(update={"stream": True})),
                timeout=request.timeout_seconds,
            ) as response:
                if response.is_error:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    raise ProviderError(
                        f"anthropic returned HTTP {response.status_code}",
                        ErrorKind.RATE_LIMIT
                        if response.status_code == 429
                        else ErrorKind.UNAVAILABLE,
                        retryable,
                    )
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = json.loads(line.removeprefix("data: "))
                    event_type = data.get("type")
                    if event_type == "message_start":
                        message = data.get("message", {})
                        response_id = message.get("id", response_id)
                        model = message.get("model", model)
                        input_tokens, cached_tokens = _usage(message.get("usage") or {})
                        usage = usage.model_copy(
                            update={"input_tokens": input_tokens, "cached_tokens": cached_tokens}
                        )
                    elif event_type == "content_block_start":
                        block = data.get("content_block", {})
                        if block.get("type") == "tool_use":
                            call_parts[data["index"]] = {
                                "id": block["id"],
                                "name": self._runtime_tool_name(block["name"]),
                                "json": "",
                            }
                    elif event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            piece = delta.get("text", "")
                            text_parts.append(piece)
                            yield ModelStreamEvent(type="delta", delta=piece)
                        elif delta.get("type") == "input_json_delta":
                            call_parts[data["index"]]["json"] += delta.get("partial_json", "")
                    elif event_type == "message_delta":
                        reason = data.get("delta", {}).get("stop_reason")
                        stop_reason = _FINISH_REASONS.get(str(reason), stop_reason)
                        usage = usage.model_copy(
                            update={"output_tokens": data.get("usage", {}).get("output_tokens", 0)}
                        )
        except httpx.TimeoutException as exc:
            raise ProviderError("anthropic stream timed out", ErrorKind.TIMEOUT, True) from exc
        calls = tuple(
            ToolCall(
                id=item["id"],
                name=item["name"],
                arguments=json.loads(item["json"] or "{}"),
            )
            for _, item in sorted(call_parts.items())
        )
        for call in calls:
            yield ModelStreamEvent(type="tool_call", tool_call=call)
        completed = ModelResponse(
            id=response_id,
            message=Message.text(Role.ASSISTANT, "".join(text_parts)),
            tool_calls=calls,
            finish_reason=FinishReason.TOOL_CALLS if calls else stop_reason,
            usage=usage,
            latency_ms=(time.monotonic() - started) * 1000,
            model=model,
            provider=self.provider_id,
        )
        yield ModelStreamEvent(type="completed", response=completed)
