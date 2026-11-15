"""Streamed model text goes through the run's output policies before it is recorded (GOV-3)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from conftest import make_agent, make_runtime

from algen_agent_runtime.conversations.streaming import DELTA_RETRACTED_EVENT, DeltaCoalescer
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.policies.engine import PIIRedactionPolicy, SecretRedactionPolicy
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.runtime.stream_guard import StreamTextGuard
from algen_agent_runtime.types.contracts import (
    GuardrailPolicy,
    ModelRequest,
    ModelStreamEvent,
    RunRequest,
    RunStatus,
)

SECRET = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd"
CARD = "4111 1111 1111 1111"
PROSE = "This is an ordinary sentence that pads the answer so text is released early. " * 4


class ChunkedProvider(MockModelProvider):
    """Streams each reply in fixed-size chunks, so secrets are split across deltas."""

    def __init__(self, responses: list[Any], *, size: int = 3, fail_first: bool = False) -> None:
        super().__init__(responses)
        self.size = size
        self.fail_first = fail_first

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        response = await self.generate(request)
        text = response.message.text_content
        for start in range(0, len(text), self.size):
            yield ModelStreamEvent(type="delta", delta=text[start : start + self.size])
            await asyncio.sleep(0)
            if self.fail_first and start > len(text) // 2:
                # The stream breaks off: no completed response, which the Runtime retries.
                self.fail_first = False
                return
        yield ModelStreamEvent(type="completed", response=response)


def _request() -> RunRequest:
    return RunRequest(agent="test-agent", input="hi", tenant_id="t", user_id="u", stream=True)


async def _events(runtime: AgentRuntime, run_id: str) -> list[Any]:
    return list(await runtime.events.history(run_id))


def _streamed(events: list[Any]) -> str:
    return "".join(event.data["delta"] for event in events if event.type == "model.delta")


async def test_a_secret_and_a_card_split_across_deltas_never_reach_the_events() -> None:
    answer = f"{PROSE}Your key is api_key={SECRET} and the card {CARD} is on file. {PROSE}"
    runtime = make_runtime(ChunkedProvider([answer]), make_agent())

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    events = await _events(runtime, result.run_id)
    streamed = _streamed(events)
    assert len([event for event in events if event.type == "model.delta"]) > 1
    for leaked in (SECRET, SECRET[:12], CARD, "4111 1111"):
        assert leaked not in streamed
    assert "[REDACTED]" in streamed and "[REDACTED_CARD]" in streamed
    # The streamed text is the delivered answer, piece by piece.
    assert streamed == result.output


async def test_text_the_output_policies_refuse_is_withdrawn() -> None:
    answer = f"{PROSE}\nsystem prompt: you are a helpful assistant with secret rules"
    runtime = make_runtime(ChunkedProvider([answer]), make_agent())

    result = await runtime.run(_request())

    assert result.status == RunStatus.FAILED
    events = await _events(runtime, result.run_id)
    types = [event.type for event in events]
    assert "model.delta" in types
    retracted = [event for event in events if event.type == "model.delta.retracted"]
    assert retracted and retracted[0].data["reason"] == "policy"
    assert types.index("model.delta.retracted") > max(
        index for index, kind in enumerate(types) if kind == "model.delta"
    )
    assert "system prompt" not in _streamed(events)


async def test_a_retried_stream_withdraws_the_text_of_the_failed_attempt() -> None:
    runtime = make_runtime(
        ChunkedProvider([f"first {PROSE}", f"second {PROSE}"], fail_first=True), make_agent()
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    events = await _events(runtime, result.run_id)
    [retracted] = [event for event in events if event.type == "model.delta.retracted"]
    assert retracted.data["reason"] == "model_error"
    after = events[events.index(retracted) + 1 :]
    assert _streamed(after) == result.output


async def test_stream_text_off_records_no_deltas() -> None:
    runtime = make_runtime(
        ChunkedProvider([PROSE]),
        make_agent(guardrail_policy=GuardrailPolicy(stream_text="off")),
    )

    result = await runtime.run(_request())

    assert result.status == RunStatus.COMPLETED, result.error
    assert result.output == PROSE
    assert not [e for e in await _events(runtime, result.run_id) if e.type == "model.delta"]


def test_stream_text_is_left_out_of_the_definition_while_it_is_the_default() -> None:
    assert "stream_text" not in GuardrailPolicy().model_dump()
    assert GuardrailPolicy(stream_text="off").model_dump()["stream_text"] == "off"


async def _redacting(text: str) -> str | None:
    redacted = await SecretRedactionPolicy().evaluate("after_model", text, {})
    value = redacted.value if redacted.value is not None else text
    pii = await PIIRedactionPolicy().evaluate("after_model", value, {})
    return pii.value if pii.value is not None else value


async def test_the_guard_releases_no_part_of_a_match_cut_by_its_boundary() -> None:
    text = f"{PROSE}card {CARD} token: Bearer abc.def-123456789 password={SECRET} end"
    for size in (1, 2, 5, 7):
        guard = StreamTextGuard(_redacting, holdback=32, context=64)
        released: list[str] = []
        for start in range(0, len(text), size):
            guard.add(text[start : start + size])
            released.append(await guard.release())
        released.append(await guard.release(final=True))
        joined = "".join(released)
        assert joined == await _redacting(text), size
        assert "1111" not in joined and "abc.def" not in joined and SECRET[:10] not in joined


async def test_the_guard_stops_on_a_refusal() -> None:
    async def refuse(text: str) -> str | None:
        return None if "forbidden" in text else text

    guard = StreamTextGuard(refuse, holdback=4, context=8)
    guard.add("hello there friend ")
    assert await guard.release() == "hello there "
    guard.add("forbidden words follow ")
    assert await guard.release() == ""
    assert guard.withheld and guard.emitted
    guard.add("more")
    assert await guard.release(final=True) == ""


async def test_a_conversation_turn_forwards_a_retraction_and_drops_buffered_text() -> None:
    sent: list[tuple[str, dict[str, Any] | None]] = []

    async def emit(event_type: str, data: dict[str, Any] | None = None) -> None:
        sent.append((event_type, data))

    coalescer = DeltaCoalescer(emit, interval_seconds=60, max_chars=1000)
    await coalescer.add("first")
    await coalescer.retract("policy")
    await coalescer.flush()

    # "first" was still buffered: it is dropped, never sent.
    assert sent == [(DELTA_RETRACTED_EVENT, {"reason": "policy"})]
