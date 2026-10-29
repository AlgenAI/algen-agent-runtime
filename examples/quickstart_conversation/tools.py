"""The order tools: a read that sends a note for the UI, and a write the customer approves."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from algen_agent_runtime.tools.authoring import read, tool, write
from algen_agent_runtime.tools.contracts import ToolContext

# Stands in for the order system. app.py resets it for each demo.
ADDRESSES: dict[str, str] = {"A-100": "4 Castle Street, Edinburgh"}


class Shipment(BaseModel):
    order_id: str
    status: str
    carrier: str
    arrives: str


class AddressChange(BaseModel):
    order_id: str
    address: str


class Gate:
    """Holds orders.track open until the user has steered, so the demo is deterministic.

    Only app.py installs one. A real run needs no gate: a steering message sent at any time
    reaches the model at the run's next step boundary.
    """

    current: Gate | None = None

    def __init__(self) -> None:
        self.run_id: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.release = asyncio.Event()


@tool(read(), name="orders.track")
async def track(order_id: str, context: ToolContext) -> Shipment:
    """Say where an order is and when it arrives."""
    # A note is for the user interface only: a tool.note run event, never seen by the model.
    await context.emit_note(
        title=f"Checked the carrier for {order_id}",
        suggestions=["Change the delivery address"],
        data={"carrier": "Parcelforce"},
    )
    gate = Gate.current
    if gate is not None:
        if not gate.run_id.done():
            gate.run_id.set_result(context.run_id)
        await gate.release.wait()
    return Shipment(order_id=order_id, status="in transit", carrier="Parcelforce", arrives="Fri")


@tool(write(), name="orders.change_address")
async def change_address(order_id: str, address: str, context: ToolContext) -> AddressChange:
    """Change where an order is delivered. The customer approves every change first."""
    ADDRESSES[order_id] = address
    await context.emit_note(title=f"Delivery address of {order_id} changed", data={})
    return AddressChange(order_id=order_id, address=address)
