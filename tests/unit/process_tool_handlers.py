"""Tool handlers for the subprocess tests: top-level functions the child process can import."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from algen_agent_runtime.exceptions.errors import ToolNotAttemptedError


def echo(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "value": arguments["value"],
        "user": context.user_id,
        "credentials": dict(context.credentials),
    }


async def echo_async(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    return {"pid": os.getpid(), "value": arguments["value"]}


def hang(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    """Write this process's id, then run far past any test's timeout."""
    Path(arguments["pid_file"]).write_text(str(os.getpid()))
    time.sleep(60)
    Path(arguments["done_file"]).write_text("done")
    return {}


def refuse(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    raise ToolNotAttemptedError("no credential, nothing sent")


def fail(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    raise ValueError("boom")
