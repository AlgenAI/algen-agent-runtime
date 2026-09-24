from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("account-service")


@mcp.tool()
def read_account(account_id: str) -> dict[str, Any]:
    """Look up an account by ID."""
    return {"account_id": account_id, "name": "Synthetic Account", "status": "active"}


@mcp.tool()
def delete_account(account_id: str) -> dict[str, Any]:
    """Delete an account by ID."""
    return {"account_id": account_id, "deleted": True}


if __name__ == "__main__":
    mcp.run(transport="stdio")
