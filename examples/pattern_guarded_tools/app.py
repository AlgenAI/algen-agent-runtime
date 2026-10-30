"""Run scripted tool calls through the Runtime's guards offline and print what each one did.

1. One run in which the model makes the mistakes the guards exist for: a repeated read, a
   discount the precheck refuses (twice in a row, so the refusal tells it to stop), a failure
   returned as data, a search past its call limit, and a wrap-up notice near the step limit.
2. Three runs by two users of one tenant reading their own orders through the tool-result
   cache: ``cache_context={"user_id"}`` keeps them apart.
3. A run by a user whose usage meter has no allowance left: the model call is refused.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from algen_agent_runtime import MeterCall, MeterRefusedError
from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import Container, build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest, RunResult, TokenUsage
from examples.pattern_guarded_tools.tools import calls_seen, handler_runs

CONFIG_PATH = Path(__file__).resolve().with_name("agent.yaml")
TENANT_ID = "example-tenant"


@dataclass(frozen=True)
class Hold:
    call: MeterCall  # settle and release get only the ticket, so keep the call in it
    credits: int


@dataclass
class CreditMeter:
    """A ``UsageMeter``: holds a call's most cost before it runs, charges what it used after."""

    allowances: dict[str, int]
    used: dict[str, int] = field(default_factory=dict)
    held: dict[str, int] = field(default_factory=dict)
    calls: list[MeterCall] = field(default_factory=list)

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> object:
        user = call.user_id or "unattributed"
        needed = max_input_tokens + max_output_tokens
        left = self.allowances.get(user, 0) - self.used.get(user, 0) - self.held.get(user, 0)
        if needed > left:
            raise MeterRefusedError(f"{user} has {left} credits left; this call may need {needed}.")
        self.held[user] = self.held.get(user, 0) + needed
        self.calls.append(call)
        return Hold(call=call, credits=needed)

    async def settle(self, ticket: object, usage: TokenUsage) -> None:
        assert isinstance(ticket, Hold)
        user = ticket.call.user_id or "unattributed"
        self.held[user] -= ticket.credits
        self.used[user] = self.used.get(user, 0) + usage.input_tokens + usage.output_tokens

    async def release(self, ticket: object) -> None:
        assert isinstance(ticket, Hold)
        user = ticket.call.user_id or "unattributed"
        self.held[user] -= ticket.credits


async def _run(container: Container, user_id: str, text: str) -> RunResult:
    return await AlgenAgentRuntimeClient(container.runtime).run(
        RunRequest(agent="pricing-assistant", input=text, tenant_id=TENANT_ID, user_id=user_id)
    )


async def _print_tool_events(container: Container, run_id: str) -> None:
    refusal = ""
    for event in await container.runtime.events.history(run_id):
        data = event.data
        if event.type == "tool.refused":
            stop = "; the note says stop calling it" if data["refusal_limit_reached"] else ""
            refusal = f", refusal {data['refusal_count']} in a row{stop}"
        elif event.type == "tool.completed":
            reason = f" ({data['reason_code']})" if data.get("reason_code") else ""
            print(f"  {data['tool']:18} {data['status']}{reason}{refusal}")
            refusal = ""
        elif event.type == "tool.wrap_up":
            print(f"  {'':18} wrap-up notice added: {data['tool_calls_left']} tool call(s) left")
        elif event.type == "budget.refused":
            print(f"  meter refused the {data['provider']}/{data['model']} call: {data['reason']}")


async def demonstrate(verbose: bool = False) -> dict[str, Any]:
    handler_runs.clear()
    calls_seen.clear()
    meter = CreditMeter(allowances={"alice": 50_000, "bob": 50_000, "carol": 0})
    # The settings declare ShopPlugin (plugin.py), which registers the shop tools.
    container = build_container(
        load_settings((CONFIG_PATH,)),
        usage_meter=meter,
        # Reserve input as if a token were three characters, not four: code and JSON run short.
        usage_meter_margin=4 / 3,
    )
    model = container.runtime.router.provider("mock")
    assert isinstance(model, MockModelProvider)
    outcome: dict[str, Any] = {}
    try:
        await container.astart()

        print("1. Guards, in one run (the model is scripted to make mistakes)")
        model.queue(
            tool_call_response("shop.stock", {"sku": "SKU-1"}),
            tool_call_response("shop.stock", {"sku": "SKU-1"}),  # repeat guard
            tool_call_response("shop.set_discount", {"sku": "SKU-1", "percent": 80}),  # precheck
            tool_call_response("shop.set_discount", {"sku": "SKU-1", "percent": 20}),  # resets
            tool_call_response("shop.set_discount", {"sku": "SKU-9", "percent": 10}),  # ok=false
            tool_call_response("shop.set_discount", {"sku": "SKU-1", "percent": 90}),
            tool_call_response("shop.set_discount", {"sku": "SKU-1", "percent": 95}),  # limit
            tool_call_response("shop.search", {"text": "kettle"}),
            tool_call_response("shop.search", {"text": "pot"}),
            tool_call_response("shop.search", {"text": "mug"}),  # past max_calls_per_tool
            "SKU-1 is now 20% off at 32.00. I could not apply a larger discount: it needs a "
            "manager.",
        )
        guarded = await _run(container, "alice", "Discount the red kettle as much as you can")
        await _print_tool_events(container, guarded.run_id)
        print(f"  run {guarded.status.value}: {guarded.output}")
        print(f"  set_discount reached its handler {len(calls_seen)} times (the wrapper saw each)")
        outcome["guards"] = guarded.status.value
        outcome["discount_handler_calls"] = len(calls_seen)

        print("\n2. Tool-result cache keyed on the user (cache_context={'user_id'})")
        for user_id in ("alice", "bob", "alice"):
            model.queue(tool_call_response("shop.my_orders", {}), f"Here are {user_id}'s orders.")
            before = handler_runs["shop.my_orders"]
            result = await _run(container, user_id, "What are my orders?")
            ran = handler_runs["shop.my_orders"] > before
            print(
                f"  {user_id:6} {result.status.value}: "
                f"{'the handler ran' if ran else 'served from the cache'}"
            )
        print(f"  my_orders handler ran {handler_runs['shop.my_orders']} times for 3 calls")
        outcome["my_orders_handler_runs"] = handler_runs["shop.my_orders"]

        print("\n3. Usage meter (MeterCall says whose call it is)")
        model.queue("This answer is never generated.")
        refused = await _run(container, "carol", "What are my orders?")
        await _print_tool_events(container, refused.run_id)
        print(f"  carol's run {refused.status.value}")
        for user_id in ("alice", "bob"):
            calls = [call for call in meter.calls if call.user_id == user_id]
            purposes = sorted({call.purpose for call in calls})
            print(
                f"  {user_id:6} {len(calls)} model calls ({', '.join(purposes)}) on "
                f"{calls[0].provider}/{calls[0].model}, {meter.used[user_id]} credits"
            )
        outcome["meter_refused"] = refused.status.value

        if verbose:
            for event in await container.runtime.events.history(guarded.run_id):
                print(event.type, event.data)
        return outcome
    finally:
        await container.aclose()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Show prechecks, tool-call guards, a scoped cache and a usage meter offline"
    )
    parser.add_argument("--verbose", action="store_true", help="Also print every run event")
    arguments = parser.parse_args(argv)
    outcome = asyncio.run(demonstrate(arguments.verbose))
    print(f"\nresult: {outcome}")


if __name__ == "__main__":
    main()
