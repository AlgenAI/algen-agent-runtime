"""The stock tools: a read, a write an approver decides, and a slow read the demo cancels."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from algen_agent_runtime.tools.authoring import read, tool, write

# Stands in for the warehouse system. app.py resets it for each round.
STOCK: dict[str, int] = {}
REORDERS: list[tuple[str, int]] = []


def reset() -> None:
    STOCK.clear()
    STOCK.update({"SKU-1": 12, "SKU-2": 0})
    REORDERS.clear()


class StockLevel(BaseModel):
    sku: str
    on_hand: int


class Reorder(BaseModel):
    sku: str
    quantity: int


@tool(read(), name="stock.level")
async def level(sku: str) -> StockLevel:
    """How many units of a SKU are on hand."""
    return StockLevel(sku=sku, on_hand=STOCK.get(sku, 0))


@tool(write(approval=True), name="stock.reorder")
async def reorder(sku: str, quantity: int) -> Reorder:
    """Order more units of a SKU from the supplier. An approver decides every order."""
    REORDERS.append((sku, quantity))
    return Reorder(sku=sku, quantity=quantity)


@tool(read(timeout=120), name="stock.audit")
async def audit(sku: str) -> StockLevel:
    """Count a SKU by hand in the warehouse. Slow: the demo cancels the run while it waits."""
    # An async handler is cancelled where it waits, so cancelling the run stops it here.
    await asyncio.sleep(60)
    return StockLevel(sku=sku, on_hand=STOCK.get(sku, 0))
