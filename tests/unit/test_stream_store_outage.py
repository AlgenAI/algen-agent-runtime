"""An event stream whose store goes down ends cleanly (found by the long soak).

The response has started, so the error cannot become a 503: the stream used to end with an
unhandled ``StoreUnavailableError`` ("Exception in ASGI application" and a traceback per open
stream during a PostgreSQL restart). It now ends after the events it delivered, and the client
reconnects with Last-Event-ID.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from test_follow_through_pauses import HEADERS, _http, _paused, _request, _runtime

from algen_agent_runtime.events.bus import InMemoryEventBus
from algen_agent_runtime.exceptions.errors import StoreUnavailableError


async def test_a_run_stream_ends_cleanly_when_the_store_goes_down() -> None:
    runtime = _runtime()
    state = await runtime.start(_request())
    await _paused(runtime, state.id)
    durable = runtime.events

    class FailingBus(InMemoryEventBus):
        async def subscribe(self, run_id: str, after: int = 0) -> Any:  # type: ignore[override]
            async for event in durable.subscribe(run_id, after):
                yield event
                if event.sequence == after + 2:
                    raise StoreUnavailableError("PostgreSQL is unavailable (test)")

    runtime.events = FailingBus()
    try:
        async with _http(runtime) as client:
            response = await asyncio.wait_for(
                client.get(f"/v1/runs/{state.id}/events", headers=HEADERS), timeout=5
            )
    finally:
        await runtime.shutdown(grace_seconds=5)

    assert response.status_code == 200
    frames = [frame for frame in response.text.strip().split("\n\n") if frame]
    ids = [int(frame.split("\n")[0].removeprefix("id: ")) for frame in frames]
    assert ids == [1, 2]
    assert json.loads(frames[-1].split("data: ")[1].split("\n")[0])["sequence"] == 2
