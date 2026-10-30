"""Four typed shop tools, each showing one control the Runtime applies around a tool call."""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from pydantic import BaseModel

from algen_agent_runtime.tools.authoring import build_tool, read, tool, write
from algen_agent_runtime.tools.contracts import (
    EarlierCall,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolHandler,
    wrap,
)

MAX_DISCOUNT_PERCENT = 30


class Product(NamedTuple):
    name: str
    price: float
    stock: int


PRODUCTS = {"SKU-1": Product("Red kettle", 40.0, 12), "SKU-2": Product("Blue teapot", 25.0, 0)}
ORDERS = {
    "alice": [{"id": "O-1", "sku": "SKU-1", "status": "shipped"}],
    "bob": [{"id": "O-7", "sku": "SKU-2", "status": "back-ordered"}],
}

# How often each handler really ran: the cache and the guards answer some calls without it.
handler_runs: Counter[str] = Counter()


class Stock(BaseModel):
    sku: str
    name: str
    units: int


class Order(BaseModel):
    id: str
    sku: str
    status: str


class DiscountResult(BaseModel):
    ok: bool
    sku: str
    price: float | None = None
    error: str | None = None


@tool(read(), name="shop.search")
def search(text: str) -> list[str]:
    """Find product SKUs whose name contains ``text``."""
    handler_runs["shop.search"] += 1
    return [sku for sku, product in PRODUCTS.items() if text.lower() in product.name.lower()]


@tool(read(), name="shop.stock")
def stock(sku: str) -> Stock:
    """Units of one product in stock."""
    handler_runs["shop.stock"] += 1
    product = PRODUCTS[sku]
    return Stock(sku=sku, name=product.name, units=product.stock)


async def discount_precheck(
    definition: ToolDefinition, arguments: Mapping[str, Any], earlier: Sequence[EarlierCall]
) -> str | None:
    """Why the discount would be refused, or None. Runs before the call and any approval."""
    del definition, earlier  # the rule depends on this call alone
    percent = arguments.get("percent", 0)
    if percent > MAX_DISCOUNT_PERCENT:
        return f"A discount above {MAX_DISCOUNT_PERCENT}% needs a manager; {percent}% was asked."
    return None


def set_discount(sku: str, percent: int) -> DiscountResult:
    """Discount one product by a whole percentage. ``ok`` is false when the SKU is unknown."""
    handler_runs["shop.set_discount"] += 1
    product = PRODUCTS.get(sku)
    if product is None:
        # An expected failure the model can fix, returned as data: recorded as failed.
        return DiscountResult(ok=False, sku=sku, error=f"No product {sku}; search for it first.")
    price = round(product.price * (100 - percent) / 100, 2)
    return DiscountResult(ok=True, sku=sku, price=price)


# build_tool is what @tool calls; use it for a function you do not want to decorate.
set_discount_tool = build_tool(
    set_discount,
    write(),
    name="shop.set_discount",
    precheck=discount_precheck,
    failure_field="ok",
)


@tool(read(), name="shop.my_orders", cache_context={"user_id"})
def my_orders(context: ToolContext) -> list[Order]:
    """The signed-in customer's orders. The result depends on who asks, so it is cached per user."""
    handler_runs["shop.my_orders"] += 1
    return [Order(**order) for order in ORDERS.get(context.user_id, [])]


calls_seen: list[str] = []


def logged(handler: ToolHandler) -> ToolHandler:
    """Middleware: note every call that reaches the handler, then run it."""

    async def run(arguments: dict[str, Any], context: ToolContext) -> Any:
        calls_seen.append(f"{context.step_id}: {arguments}")
        result = handler(arguments, context)
        return await result if inspect.isawaitable(result) else result

    return run


def shop_tools() -> tuple[Tool, ...]:
    # wrap() swaps only the handler; the definition, the precheck and every other hook stay.
    logged_discount = wrap(set_discount_tool, logged)
    assert logged_discount.precheck is set_discount_tool.precheck
    assert logged_discount.definition == set_discount_tool.definition
    return search, stock, logged_discount, my_orders
