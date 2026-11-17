"""A host that follows a streamed run, offline: retracted text, reconnecting, and cancellation.

1. A streamed run whose first answer fails verification. The host shows the streamed text, then
   discards it on ``model.delta.retracted`` and shows the repaired answer. It drops its
   subscription halfway and reconnects from the last sequence it saw: nothing is repeated.
2. The user cancels a run while its async tool waits. The tool is cancelled where it waits, and
   the stream ends with exactly one ``run.cancelled``.
3. The host's own deadline around ``Runtime.wait``: since 0.2.0b5 the cancelled waiter raises
   ``CancelledError`` (here ``TimeoutError`` from ``asyncio.timeout``), and the run ends
   ``cancelled`` instead of the host carrying on as if it had finished.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.events.contracts import (
    RUN_END_EVENT_TYPES,
    RUN_PAUSE_EVENT_TYPES,
    RunEvent,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.runtime.runtime import AgentRuntime
from algen_agent_runtime.types.contracts import RequestOverrides, RunRequest
from algen_agent_runtime_examples.pattern_run_follower.plugin import CANCELLED, WAITING

CONFIG_PATH = Path(__file__).resolve().with_name("agent.yaml")
TENANT_ID = "example-tenant"
USER_ID = "agent-desk"
AGENT = "ticket-assistant"
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"ticket_id": {"type": "string"}, "status": {"type": "string"}},
    "required": ["ticket_id", "status"],
    "additionalProperties": False,
}


def say(text: str = "") -> None:
    print(text, flush=True)


class StreamView:
    """What a UI keeps for one run: the text shown so far and the last event's sequence.

    ``model.delta`` appends text. ``model.delta.retracted`` means the text streamed so far is not
    the answer the run will deliver (a policy refused it, verification failed, the model call was
    retried), so the view discards it and later deltas start from empty. The run's output on
    ``run.completed`` is the record; deltas are provisional.
    """

    def __init__(self) -> None:
        self.run_id = ""
        self.text = ""
        self.last_sequence = 0
        self.retractions: list[str] = []
        self.types: list[str] = []

    def apply(self, event: RunEvent) -> None:
        if event.sequence <= self.last_sequence:
            raise AssertionError(f"event {event.sequence} was delivered twice")
        self.run_id = event.run_id
        self.last_sequence = event.sequence
        self.types.append(event.type)
        if event.type == "model.delta":
            self.text += event.data["delta"]
            say(f"  [{event.sequence:>2}] model.delta            shown: {self.text!r}")
        elif event.type == "model.delta.retracted":
            reason = event.data["reason"]
            self.retractions.append(reason)
            say(
                f"  [{event.sequence:>2}] model.delta.retracted  reason={reason}: discard the text shown"
            )
            self.text = ""
        elif event.type == "verification.completed":
            say(f"  [{event.sequence:>2}] verification.completed passed={event.data.get('passed')}")
        elif event.type == "tool.started":
            say(f"  [{event.sequence:>2}] tool.started           {event.data.get('tool')}")
        elif event.type in RUN_END_EVENT_TYPES or event.type in RUN_PAUSE_EVENT_TYPES:
            say(f"  [{event.sequence:>2}] {event.type}")


async def follow(
    view: StreamView, events: AsyncIterator[RunEvent], *, stop_after: str | None = None
) -> None:
    async for event in events:
        view.apply(event)
        if event.type == stop_after:
            return


def request(text: str, **values: Any) -> RunRequest:
    return RunRequest(agent=AGENT, input=text, tenant_id=TENANT_ID, user_id=USER_ID, **values)


async def retracted_and_reconnected(
    runtime: AgentRuntime, model: MockModelProvider
) -> dict[str, Any]:
    say("== 1. Streamed text that is retracted, and a reconnect ==")
    model.queue(
        "Ticket T-1 is still open; the customer has not answered yet.",  # fails the schema
        json.dumps({"ticket_id": "T-1", "status": "open"}),
    )
    client = AlgenAgentRuntimeClient(runtime)
    view = StreamView()
    # client.stream starts the run with stream=True and yields its events until it ends or pauses.
    events = client.stream(
        request(
            "Summarise ticket T-1 as JSON.",
            overrides=RequestOverrides(response_schema=SUMMARY_SCHEMA),
        )
    )
    await follow(view, events, stop_after="model.delta.retracted")
    await events.aclose()  # the connection drops here
    say(f"  -- disconnected after {view.last_sequence}; reconnect with after={view.last_sequence}")
    # Over HTTP: GET /v1/runs/{run_id}/events with Last-Event-ID set to the last id received.
    await follow(view, client.stream_events(view.run_id, after=view.last_sequence))
    result = await runtime.wait(view.run_id, TENANT_ID)
    say(f"  shown at the end: {view.text!r}")
    say(f"  run output (the record): {result.output!r}")
    return {
        "status": result.status.value,
        "retractions": view.retractions,
        # The mock streams word by word with a trailing space; the stored output is trimmed.
        "shown_is_output": view.text.strip() == result.output,
    }


async def cancelled_by_the_user(runtime: AgentRuntime, model: MockModelProvider) -> dict[str, Any]:
    say("\n== 2. The user cancels while a tool runs ==")
    model.queue(tool_call_response("ticket.lookup", {"ticket_id": "T-2"}))
    client = AlgenAgentRuntimeClient(runtime)
    view = StreamView()
    async for event in client.stream(request("What is the status of ticket T-2?")):
        view.apply(event)
        if event.type == "tool.started":
            # tool.started is sent as the call begins; wait until the handler is waiting. A run
            # cancelled before the handler starts records the call failed: nothing ran.
            await WAITING.wait()
            # Over HTTP: DELETE /v1/runs/{run_id} (the run's user, or runs:admin).
            state = await runtime.cancel(view.run_id, TENANT_ID)
            say(f"  -- runtime.cancel -> {state.status.value}")
    # The stream ended on run.cancelled; wait for the drive to finish standing down.
    result = await runtime.wait(view.run_id, TENANT_ID)
    say(f"  the tool's handler was cancelled where it waited: {CANCELLED == ['T-2']}")
    return {
        "status": result.status.value,
        "run_cancelled_events": view.types.count("run.cancelled"),
        "last_event": view.types[-1],
        "tool_cancelled": CANCELLED == ["T-2"],
    }


async def host_deadline(runtime: AgentRuntime, model: MockModelProvider) -> dict[str, Any]:
    say("\n== 3. The host's own deadline around Runtime.wait ==")
    model.queue(tool_call_response("ticket.lookup", {"ticket_id": "T-3"}))
    state = await runtime.start(request("What is the status of ticket T-3?"))
    timed_out = False
    try:
        async with asyncio.timeout(0.2):
            # Cancelling the task that awaits Runtime.wait (or Runtime.run) cancels the run.
            await runtime.wait(state.id, TENANT_ID)
    except TimeoutError:
        # Since 0.2.0b5 the waiter re-raises CancelledError, which asyncio.timeout turns into
        # TimeoutError. Before, wait returned the cancelled run's result as if it had finished.
        # Code that catches CancelledError itself must re-raise it.
        timed_out = True
    status = (await runtime.status(state.id, TENANT_ID)).status.value
    say(f"  asyncio.timeout raised TimeoutError: {timed_out}; the run is {status}")
    return {"timed_out": timed_out, "status": status}


async def run() -> dict[str, Any]:
    CANCELLED.clear()
    container = build_container(load_settings((CONFIG_PATH,)))
    runtime = container.runtime
    assert isinstance(runtime, AgentRuntime)
    model = runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    try:
        await container.astart()
        return {
            "retracted": await retracted_and_reconnected(runtime, model),
            "cancelled": await cancelled_by_the_user(runtime, model),
            "deadline": await host_deadline(runtime, model),
        }
    finally:
        await container.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.parse_args(argv)
    outcome = asyncio.run(run())
    print(f"\nresult: {outcome}")


if __name__ == "__main__":
    main()
