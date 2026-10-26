"""A CRM toolkit: one read tool and one approval-gated write tool sharing one client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from algen_agent_runtime.tools.authoring import Toolkit, read, write
from algen_agent_runtime.tools.contracts import ToolContext


class Account(BaseModel):
    id: str
    domain: str
    owner: str


class Receipt(BaseModel):
    account_id: str
    owner: str
    previous_owner: str


class InMemoryCrm:
    """Stands in for a real CRM client; a real one would read its token from env://."""

    def __init__(self) -> None:
        self.accounts = {"acc-1": Account(id="acc-1", domain="acme.com", owner="dana")}
        self.receipts: dict[str, Receipt] = {}

    async def aclose(self) -> None:
        self.accounts.clear()


async def open_crm(secrets: Callable[[str], Awaitable[str]]) -> InMemoryCrm:
    # A real toolkit resolves its credentials here, when the client opens:
    # token = await secrets("env://CRM_API_TOKEN"), with env=("CRM_API_TOKEN",) on the Toolkit.
    return InMemoryCrm()


crm = Toolkit("crm", client=open_crm)


@crm.tool(read())
async def find_account(domain: str) -> Account:
    """Find one CRM account by its web domain, for example acme.com."""
    client: InMemoryCrm = await crm.client()
    for account in client.accounts.values():
        if account.domain == domain:
            return account
    raise ValueError(f"no account for {domain}")


@crm.tool(write(approval=True))
async def update_owner(account_id: str, owner: str, context: ToolContext) -> Receipt:
    """Change the owner of one CRM account. A person approves every call first."""
    client: InMemoryCrm = await crm.client()
    # The idempotency key makes a replayed call return the first receipt instead of
    # changing the account twice.
    if context.idempotency_key in client.receipts:
        return client.receipts[context.idempotency_key]
    account = client.accounts[account_id]
    receipt = Receipt(account_id=account_id, owner=owner, previous_owner=account.owner)
    client.accounts[account_id] = account.model_copy(update={"owner": owner})
    client.receipts[context.idempotency_key] = receipt
    return receipt
