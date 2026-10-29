"""A two-turn conversation, offline, printed as the event stream a chat UI would render.

Turn 1 streams its answer (``assistant.message.delta``); while its tool runs, the tool sends a
note for the UI (``ToolContext.emit_note``) and the user steers the run (``runtime.steer``).
Turn 2 asks to change something: the turn pauses for approval, the user decides, and the same
assistant message resumes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.conversations import (
    ConversationApprovalDecision,
    ConversationService,
    MessageStatus,
)
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.runtime import AgentRuntime
from examples.quickstart_conversation.tools import ADDRESSES, Gate

CONFIG_PATH = Path(__file__).resolve().with_name("agent.yaml")
TENANT_ID = "example-tenant"
USER_ID = "example-user"
STEERING = "Also tell me the delivery window, please."
NEW_ADDRESS = "12 Harbour Road, Leith"


class Transcript:
    """Prints the conversation's events, and the run events a UI also shows, as they arrive."""

    def __init__(self) -> None:
        self.seen: list[str] = []
        self._changed = asyncio.Condition()
        self._streaming = False

    async def follow_conversation(self, service: ConversationService, conversation_id: str) -> None:
        async for event in service.events.subscribe(conversation_id):
            self._print_conversation_event(event.type, event.data)
            await self._record(event.type)

    async def follow_run(self, runtime: AgentRuntime, run_id: str) -> None:
        async for event in runtime.events.subscribe(run_id):
            if event.type == "tool.note":
                self.say(f"  [tool note] {event.data['tool']}: {event.data['title']}")
                if event.data["suggestions"]:
                    self.say(f"              suggestions: {', '.join(event.data['suggestions'])}")
            elif event.type == "steer.delivered":
                self.say(f"  [steering] {event.data['count']} message(s) delivered to the model")
            elif event.type == "tool.started":
                self.say(f"  [tool] {event.data.get('tool')} {event.data.get('status')}")

    async def until(self, *event_types: str) -> str:
        """Wait for the next event of one of ``event_types``, from this point on."""
        start = len(self.seen)
        async with self._changed:
            await self._changed.wait_for(
                lambda: any(seen in event_types for seen in self.seen[start:])
            )
        return next(seen for seen in self.seen[start:] if seen in event_types)

    async def _record(self, event_type: str) -> None:
        async with self._changed:
            self.seen.append(event_type)
            self._changed.notify_all()

    def _print_conversation_event(self, event_type: str, data: dict[str, Any]) -> None:
        if event_type == "conversation.message.accepted":
            self.say(f"\nuser> {data['text']}")
        elif event_type == "assistant.message.started":
            self.say("  [assistant] started")
        elif event_type == "assistant.message.delta":
            if not self._streaming:
                print("assistant> ", end="", flush=True)
                self._streaming = True
            print(data["delta"], end="", flush=True)
        elif event_type == "assistant.message.awaiting_approval":
            self.say("  [assistant] waiting for approval")
        elif event_type == "assistant.message.resumed":
            self.say(f"  [assistant] resumed: {data.get('decision')} by {data.get('decided_by')}")
        elif event_type == "assistant.message.completed":
            self.say("  [assistant] completed")
        elif event_type.startswith("assistant.message."):
            self.say(f"  [assistant] {event_type.rsplit('.', 1)[-1]}")

    def say(self, text: str) -> None:
        if self._streaming:
            print()
            self._streaming = False
        print(text, flush=True)


async def run(decision: str = "approved") -> dict[str, Any]:
    # The settings declare OrdersPlugin (plugin.py), which registers the order tools.
    ADDRESSES["A-100"] = "4 Castle Street, Edinburgh"
    gate = Gate.current = Gate()
    container = build_container(load_settings((CONFIG_PATH,)))
    model = container.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    # Script the model offline. With a real provider, drop the queue and the model decides.
    model.queue(
        tool_call_response("orders.track", {"order_id": "A-100"}),
        "Order A-100 is in transit with Parcelforce and arrives on Friday, "
        "in the 09:00 to 13:00 delivery window.",
        tool_call_response("orders.change_address", {"order_id": "A-100", "address": NEW_ADDRESS}),
        f"Done: order A-100 will be delivered to {NEW_ADDRESS}.",
    )
    runtime = container.runtime
    assert isinstance(runtime, AgentRuntime)
    service = container.conversations
    transcript = Transcript()
    followers: list[asyncio.Task[None]] = []
    try:
        await container.astart()
        conversation = await service.create(
            tenant_id=TENANT_ID, user_id=USER_ID, agent="order-assistant"
        )
        followers.append(
            asyncio.create_task(transcript.follow_conversation(service, conversation.id))
        )

        # Turn 1: streamed, with a tool note and a steering message sent while the tool runs.
        await service.submit(conversation.id, TENANT_ID, USER_ID, "Where is my order A-100?")
        run_id = await gate.run_id
        followers.append(asyncio.create_task(transcript.follow_run(runtime, run_id)))
        await asyncio.sleep(0.01)  # let the follower print the run's events so far
        steered = await runtime.steer(run_id, TENANT_ID, STEERING)
        transcript.say(f"user (steering run {run_id[:8]})> {steered.text}")
        gate.release.set()
        await transcript.until("assistant.message.completed", "assistant.message.failed")
        model_saw = model.requests[-1].messages[-1].text_content
        transcript.say(f"  [model] its next call ended with the steering message: {model_saw!r}")

        # Turn 2: the write tool needs approval, so the turn pauses and then resumes.
        await service.submit(
            conversation.id, TENANT_ID, USER_ID, f"Please deliver it to {NEW_ADDRESS} instead."
        )
        await transcript.until("assistant.message.awaiting_approval", "assistant.message.failed")
        paused = (await service.messages(conversation.id, TENANT_ID))[-1]
        pending = paused.pending
        assert pending is not None and pending.run_id is not None
        followers.append(asyncio.create_task(transcript.follow_run(runtime, pending.run_id)))
        transcript.say(
            f"  [approval] {pending.tool}: {pending.proposed_action}\n"
            f"             parameters: {pending.redacted_parameters}\n"
            f"user> ({decision})"
        )
        await service.respond(
            conversation.id,
            pending.turn_id,
            TENANT_ID,
            USER_ID,
            ConversationApprovalDecision(
                decision="approved" if decision == "approved" else "rejected",
                reason=None if decision == "approved" else "Keep the original address.",
            ),
        )
        ended = await transcript.until("assistant.message.completed", "assistant.message.failed")
        final = (await service.messages(conversation.id, TENANT_ID))[-1]
        await asyncio.sleep(0.05)  # let the run follower print the last events
        return {
            "turn_2": ended.rsplit(".", 1)[-1],
            "status": final.status.value,
            "answer": final.text_content if final.status == MessageStatus.COMPLETED else None,
            "address": ADDRESSES["A-100"],
        }
    finally:
        for follower in followers:
            follower.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await follower
        Gate.current = None
        await container.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run a streamed conversation offline: steering, a tool note and an approval"
    )
    parser.add_argument("--reject", action="store_true", help="Reject the address change")
    arguments = parser.parse_args(argv)
    outcome = asyncio.run(run("rejected" if arguments.reject else "approved"))
    print(f"\nresult: {outcome}")


if __name__ == "__main__":
    main()
