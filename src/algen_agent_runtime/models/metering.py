"""Charge for every model call the runtime dispatches.

A deployment that bills or rations model use passes a ``UsageMeter`` to
``build_container(usage_meter=...)`` (or ``ModelRouter(usage_meter=...)``). The router reserves
the most a call could cost before it reaches a provider and settles what the provider reports
afterwards, so steps, retries, fallbacks, repairs, streaming and compaction are all charged. A
call that fails is released without charge.

A meter refuses a call by raising ``MeterRefusedError`` from ``reserve``. The router does not
try another candidate and the runtime does not retry; the run fails with the refusal message.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from algen_agent_runtime.types.contracts import TokenUsage

# Reserved output for a request without ``max_output_tokens``. The runtime always sets one for
# agent steps and compaction; this only covers direct router use.
DEFAULT_RESERVED_OUTPUT_TOKENS = 4_096


@runtime_checkable
class UsageMeter(Protocol):
    """Reserve, settle and release the token cost of each model call."""

    async def reserve(self, *, max_input_tokens: int, max_output_tokens: int) -> object:
        """Hold the most a call could cost and return a ticket for it.

        ``max_input_tokens`` is the router's estimate of the request sent to the selected
        model (``estimate_request_tokens``); ``max_output_tokens`` is the request's output cap.
        Raise ``MeterRefusedError`` to refuse the call.
        """
        ...

    async def settle(self, ticket: object, usage: TokenUsage) -> None:
        """Charge a completed call what the provider reported it used."""
        ...

    async def release(self, ticket: object) -> None:
        """Drop the reservation of a call that failed or was cancelled, without charge."""
        ...
