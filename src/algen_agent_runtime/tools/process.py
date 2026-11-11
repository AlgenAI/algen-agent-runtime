"""Tool handlers that run in a subprocess, so a timeout really stops them.

A tool whose definition sets ``execution="process"`` runs each call in a fresh interpreter
(``multiprocessing``'s ``spawn`` start method, on every platform: a forked copy of a threaded
event loop process is unsafe). When the caller's deadline passes, the process is killed: the call
stops on schedule and nothing of it keeps running. A side effect it had already sent still
happened, so a timed-out call that writes is recorded ``indeterminate``, as in thread mode.

What it needs and costs:

- The handler is sent to the child by reference, so it must be importable there: a function
  defined at the top level of a module (not a lambda, a closure, a ``@tool`` function or a
  wrapped handler). :meth:`ToolRegistry.register` refuses a tool whose handler cannot be pickled.
- The arguments, the :class:`ToolContext` and the result must pickle. The context's caller
  credentials are passed to the child explicitly (they never pickle with the context);
  ``emit_note`` and ``read_attachment`` are not available in the child.
- Each call starts an interpreter that imports the handler's module. That start-up (typically
  hundreds of milliseconds) counts against ``timeout_seconds``.
"""

from __future__ import annotations

import asyncio
import inspect
import multiprocessing
import pickle
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from typing import Any

from algen_agent_runtime.exceptions.errors import ToolExecutionError, ToolNotAttemptedError
from algen_agent_runtime.tools.contracts import ToolContext, ToolHandler, read_only_credentials

_SPAWN = multiprocessing.get_context("spawn")


def check_process_handler(name: str, handler: ToolHandler) -> None:
    """Raise ``ValueError`` when ``handler`` cannot be sent to a subprocess."""
    try:
        pickle.dumps(handler)
    except Exception as exc:
        raise ValueError(
            f"tool {name!r} runs in a subprocess (execution='process'), so its handler must be "
            "a function defined at the top level of an importable module; "
            f"it cannot be pickled ({type(exc).__name__})"
        ) from exc


async def run_in_process(
    name: str, handler: ToolHandler, arguments: dict[str, Any], context: ToolContext
) -> Any:
    """Run ``handler(arguments, context)`` in a new process and return its result.

    Cancelling the wait (a timeout) kills the process before the cancellation completes.
    """
    child_context = context.model_copy()
    child_context._note_sink = None
    child_context._attachment_loader = None
    try:
        payload = pickle.dumps((handler, arguments, child_context, dict(context.credentials)))
    except Exception as exc:
        raise ToolNotAttemptedError(
            f"tool {name} could not be sent to its subprocess ({type(exc).__name__})"
        ) from exc
    receiver, sender = _SPAWN.Pipe(duplex=False)
    process = _SPAWN.Process(
        target=_child, args=(sender, payload), name=f"algen-tool-{name}", daemon=True
    )
    try:
        process.start()
    except Exception as exc:
        receiver.close()
        sender.close()
        raise ToolNotAttemptedError(
            f"tool {name} could not start its subprocess ({type(exc).__name__})"
        ) from exc
    sender.close()
    try:
        outcome = await asyncio.to_thread(_receive, receiver, process)
    except BaseException:
        # A timeout or a cancelled run: the work stops here, not whenever it would have ended.
        process.kill()
        raise
    if outcome is None:
        raise ToolExecutionError(
            f"tool {name} subprocess exited without a result (exit code {process.exitcode})"
        )
    status, value = outcome
    if status == "error":
        raise value
    return value


def _receive(receiver: Connection, process: BaseProcess) -> tuple[str, Any] | None:
    """The child's ``(status, value)``, or None when it died first; reaps the child either way.

    It returns once the child sends or dies, so killing the child also ends this thread.
    """
    try:
        try:
            return receiver.recv()  # type: ignore[no-any-return]
        except EOFError:
            return None
        except Exception as exc:
            # The result or the exception arrived but could not be rebuilt here.
            return "error", ToolExecutionError(
                f"tool subprocess result could not be read ({type(exc).__name__})"
            )
    finally:
        receiver.close()
        process.join()


def _child(sender: Connection, payload: bytes) -> None:
    """The subprocess: run the handler once and send back its result or its exception."""
    message: tuple[str, Any]
    try:
        handler, arguments, context, credentials = pickle.loads(payload)
        if credentials:
            context = context.model_copy(update={"credentials": read_only_credentials(credentials)})
        result = handler(arguments, context)
        if inspect.isawaitable(result):
            result = asyncio.run(_wait(result))
        message = ("ok", result)
    except BaseException as exc:
        message = ("error", exc)
    try:
        sender.send(message)
    except Exception as exc:
        sender.send(
            (
                "error",
                ToolExecutionError(
                    f"tool subprocess outcome could not be returned ({type(exc).__name__})"
                ),
            )
        )
    finally:
        sender.close()


async def _wait(awaitable: Any) -> Any:
    return await awaitable
