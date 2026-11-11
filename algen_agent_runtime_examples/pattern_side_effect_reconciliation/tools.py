"""A billing tool with reconcile and compensation hooks, over an in-process fake billing system."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from algen_agent_runtime.exceptions import ToolNotAttemptedError
from algen_agent_runtime.tools.authoring import Toolkit, external
from algen_agent_runtime.tools.contracts import (
    ReconcileOutcome,
    ToolContext,
    ToolExecutionRecord,
    ToolResult,
)

#: How the next request to the fake billing system goes wrong, if at all.
Fault = Literal["none", "timeout_after_apply", "timeout_before_apply"]


class Credit(BaseModel):
    credit_id: str
    account_id: str
    amount_cents: int


@dataclass
class FakeBillingSystem:
    """Stands in for an external billing API that deduplicates by idempotency key.

    ``fault`` simulates the case reconciliation exists for: the request reached the system (or
    did not), and then the connection timed out, so the caller cannot tell which.
    """

    credits: dict[str, Credit] = field(default_factory=dict)
    voided: set[str] = field(default_factory=set)
    locked_accounts: set[str] = field(default_factory=set)
    fault: Fault = "none"
    requests: int = 0

    def apply_credit(self, key: str, account_id: str, amount_cents: int) -> Credit:
        self.requests += 1
        fault, self.fault = self.fault, "none"
        if fault == "timeout_before_apply":
            raise TimeoutError("billing API timed out")
        credit = self.credits.get(key)
        if credit is None:
            credit = Credit(
                credit_id=f"cr-{len(self.credits) + 1:04d}",
                account_id=account_id,
                amount_cents=amount_cents,
            )
            self.credits[key] = credit
        if fault == "timeout_after_apply":
            # The credit is recorded, but the response never reaches the caller.
            raise TimeoutError("billing API timed out")
        return credit

    def find_credit(self, key: str) -> Credit | None:
        """Look a request up by its idempotency key, as a real billing API would allow."""
        return self.credits.get(key)

    def void_credit(self, key: str) -> None:
        credit = self.credits.get(key)
        if credit is not None:
            self.voided.add(credit.credit_id)


def open_billing(secrets: Callable[[str], Awaitable[str]]) -> FakeBillingSystem:
    # A real toolkit resolves its credentials here, e.g. await secrets("env://BILLING_API_TOKEN").
    return FakeBillingSystem(locked_accounts={"acct-9009"})


billing = Toolkit("billing", client=open_billing)


async def find_credit(record: ToolExecutionRecord, context: ToolContext) -> ReconcileOutcome:
    """Ask the billing system whether the call took effect. Read-only: it may run often."""
    system: FakeBillingSystem = await billing.client()
    credit = system.find_credit(record.idempotency_key)
    if credit is None:
        # Absent now does not prove it never arrived (it may still be in flight upstream), so
        # say unknown and let an operator decide.
        return ReconcileOutcome(status="unknown", detail="no credit under this key yet")
    return ReconcileOutcome(
        status="completed",
        value=credit.model_dump(),
        result_reference=credit.credit_id,
        detail="credit found by idempotency key",
    )


async def void_credit(result: ToolResult, context: ToolContext) -> None:
    """Undo the credit. Idempotent by key; copes with a credit that never happened."""
    system: FakeBillingSystem = await billing.client()
    system.void_credit(context.idempotency_key)


@billing.tool(external(timeout=10), reconcile=find_credit, compensation=void_credit)
async def issue_credit(account_id: str, amount_cents: int, context: ToolContext) -> Credit:
    """Credit an account, in cents. The billing system deduplicates by idempotency key."""
    system: FakeBillingSystem = await billing.client()
    if account_id in system.locked_accounts:
        # Refused before anything was sent: the ledger records the call failed, not
        # indeterminate, so nobody has to reconcile it.
        raise ToolNotAttemptedError(f"account {account_id} is locked; nothing was sent")
    return system.apply_credit(context.idempotency_key, account_id, amount_cents)
