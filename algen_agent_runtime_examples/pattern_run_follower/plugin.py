from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from algen_agent_runtime.plugins.sdk import PluginManifest, plugin
from algen_agent_runtime.tools.authoring import read, tool

#: Set when ticket.lookup was cancelled where it waited (app.py reports it).
CANCELLED: list[str] = []
#: Set once ticket.lookup is waiting, so the demo cancels a call that is running.
WAITING = asyncio.Event()


class Ticket(BaseModel):
    ticket_id: str
    status: str


@tool(read(timeout=120), name="ticket.lookup")
async def lookup(ticket_id: str) -> Ticket:
    """Read a ticket from the help desk. Slow on purpose: the demo cancels a run while it waits."""
    WAITING.set()
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        # An async handler is cancelled where it waits; clean up, then let the cancellation go on.
        CANCELLED.append(ticket_id)
        raise
    return Ticket(ticket_id=ticket_id, status="open")


@plugin(PluginManifest(name="example.tickets", version="1.0.0", publisher="algen-examples"))
class TicketPlugin:
    """Registers ticket.lookup; agent.yaml names this plugin in domain_plugins."""

    def register(self, context: Any) -> None:
        context.tools.register(lookup)
