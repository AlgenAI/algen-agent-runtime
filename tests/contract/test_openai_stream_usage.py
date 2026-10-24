from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.models.providers.adapters import MistralProvider, OpenAIProvider
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.types.contracts import (
    FinishReason,
    Message,
    ModelCapabilities,
    ModelRequest,
    Role,
)


class _Secrets:
    async def get(self, reference: str) -> str:
        return "test-key"


def _sse(chunks: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"data: {json.dumps(chunk)}" for chunk in chunks) + "\n\ndata: [DONE]\n\n"


def _transport(chunks: list[dict[str, Any]], sent: list[dict[str, Any]]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=_sse(chunks))

    return httpx.MockTransport(respond)


def _delta(**delta: Any) -> dict[str, Any]:
    return {"id": "c1", "model": "gpt-test", "choices": [{"index": 0, "delta": delta}]}


_USAGE_CHUNK = {
    "id": "c1",
    "model": "gpt-test",
    "choices": [],
    "usage": {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 3},
    },
}
_FINISH = {"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
_REQUEST = ModelRequest(messages=(Message.text(Role.USER, "hi"),), stream=True)


async def _completed(provider: OpenAICompatibleProvider, request: ModelRequest = _REQUEST):
    events = [event async for event in provider.stream(request)]
    assert events[-1].type == "completed" and events[-1].response is not None
    return events, events[-1].response


def _openai(chunks: list[dict[str, Any]], sent: list[dict[str, Any]], **kwargs: Any):
    return OpenAIProvider(
        "env://OPENAI_API_KEY",
        "gpt-test",
        secret_provider=_Secrets(),
        client=httpx.AsyncClient(transport=_transport(chunks, sent)),
        **kwargs,
    )


async def test_usage_from_the_final_usage_only_chunk_reaches_the_response() -> None:
    sent: list[dict[str, Any]] = []
    provider = _openai([_delta(content="Hel"), _delta(content="lo"), _FINISH, _USAGE_CHUNK], sent)

    events, response = await _completed(provider)

    assert sent[0]["stream_options"] == {"include_usage": True}
    assert [event.delta for event in events if event.type == "delta"] == ["Hel", "lo"]
    assert response.message.text_content == "Hello"
    assert response.finish_reason == FinishReason.STOP
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 7)
    assert response.usage.cached_tokens == 3


async def test_tool_call_deltas_are_unaffected_by_the_usage_chunk() -> None:
    sent: list[dict[str, Any]] = []
    call = {"index": 0, "id": "t1", "function": {"name": "lookup", "arguments": '{"id": 4}'}}
    provider = _openai([_delta(tool_calls=[call]), _USAGE_CHUNK], sent)

    _, response = await _completed(provider)

    assert response.tool_calls[0].name == "lookup"
    assert response.tool_calls[0].arguments == {"id": 4}
    assert response.usage.total_tokens == 18


async def test_a_server_that_sends_no_usage_completes_with_zero_usage() -> None:
    sent: list[dict[str, Any]] = []

    _, response = await _completed(_openai([_delta(content="ok"), _FINISH], sent))

    assert response.message.text_content == "ok"
    assert response.usage.total_tokens == 0


async def test_non_streaming_calls_never_send_stream_options() -> None:
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "model": "gpt-test",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = OpenAIProvider(
        "env://OPENAI_API_KEY",
        "gpt-test",
        secret_provider=_Secrets(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    await provider.generate(ModelRequest(messages=(Message.text(Role.USER, "hi"),)))

    assert "stream_options" not in sent[0]


async def test_stream_usage_can_be_turned_off_for_servers_that_reject_it() -> None:
    sent: list[dict[str, Any]] = []

    await _completed(_openai([_delta(content="ok")], sent, stream_usage=False))

    assert "stream_options" not in sent[0]


async def test_explicit_stream_options_from_extensions_are_kept() -> None:
    sent: list[dict[str, Any]] = []
    request = _REQUEST.model_copy(
        update={"extensions": {"stream_options": {"include_obfuscation": False}}}
    )

    await _completed(_openai([_delta(content="ok")], sent), request)

    assert sent[0]["stream_options"] == {"include_usage": True, "include_obfuscation": False}


async def test_mistral_does_not_send_stream_options_by_default() -> None:
    sent: list[dict[str, Any]] = []
    provider = MistralProvider(
        "env://MISTRAL_API_KEY",
        secret_provider=_Secrets(),
        client=httpx.AsyncClient(transport=_transport([_delta(content="ok")], sent)),
    )

    await _completed(provider)

    assert "stream_options" not in sent[0]


@pytest.mark.parametrize(("configured", "expected"), [(None, True), (False, False)])
def test_provider_setting_reaches_the_adapter(configured: bool | None, expected: bool) -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {
                "compat": {
                    "type": "openai_compatible",
                    "base_url": "http://127.0.0.1:8001/v1",
                    "default_model": "local",
                    "capabilities": ModelCapabilities(streaming=True).model_dump(),
                    **({} if configured is None else {"stream_usage": configured}),
                }
            }
        }
    )

    provider = build_container(settings).runtime.router._providers["compat"]

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider._stream_usage is expected
