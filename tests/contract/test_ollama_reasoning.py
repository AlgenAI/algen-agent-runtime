"""Ollama: private reasoning that leaks into ``content`` never reaches the reply (#83 item 7)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from algen_agent_runtime.models.providers.adapters import OllamaProvider, strip_leaked_reasoning
from algen_agent_runtime.types.contracts import Message, ModelRequest, Role, ToolSpec

_TOOLS = (ToolSpec(name="mail.read", description="Read mail", input_schema={"type": "object"}),)


@pytest.mark.parametrize(
    ("content", "reply"),
    [
        ("<think>The user wants X.</think>\n\nHere is X.", "Here is X."),
        ("The user wants X, so I answer.\n</think>\n\nHere is X.", "Here is X."),
        ("  <think>still thinking", ""),
        ("Plain answer.", "Plain answer."),
        ("Use the <think> tag like </think> this.", "Use the <think> tag like </think> this."),
    ],
)
def test_leaked_reasoning_is_stripped(content: str, reply: str) -> None:
    assert strip_leaked_reasoning(content) == reply


def _provider(respond: Any) -> OllamaProvider:
    return OllamaProvider(
        "qwen3:4b", client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )


async def test_a_reply_drops_reasoning_before_a_bare_close_tag() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        message = {"role": "assistant", "content": "Let me think.\n</think>\n\nDone."}
        return httpx.Response(
            200, json={"id": "r1", "model": "qwen3:4b", "choices": [{"message": message}]}
        )

    response = await _provider(respond).generate(
        ModelRequest(messages=(Message.text(Role.USER, "hi"),), tools=_TOOLS)
    )

    assert response.message.text_content == "Done."


def _sse(*texts: str) -> str:
    chunks = [{"id": "c1", "choices": [{"index": 0, "delta": {"content": t}}]} for t in texts]
    chunks.append({"id": "c1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    return "\n\n".join(f"data: {json.dumps(c)}" for c in chunks) + "\n\ndata: [DONE]\n\n"


@pytest.mark.parametrize(
    ("tools", "parts", "reply"),
    [
        (_TOOLS, ("I should ", "call a tool.", "\n</th", "ink>\n", "Done."), "Done."),
        ((), ("<thi", "nk>reasoning</think>", "\nDone", "."), "Done."),
        ((), ("Hello", " there."), "Hello there."),
    ],
)
async def test_a_stream_never_sends_reasoning(
    tools: tuple[ToolSpec, ...], parts: tuple[str, ...], reply: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_sse(*parts))

    events = [
        event
        async for event in _provider(respond).stream(
            ModelRequest(messages=(Message.text(Role.USER, "hi"),), tools=tools, stream=True)
        )
    ]

    deltas = "".join(event.delta or "" for event in events if event.type == "delta")
    assert deltas == reply
    assert events[-1].response is not None
    assert events[-1].response.message.text_content == reply
