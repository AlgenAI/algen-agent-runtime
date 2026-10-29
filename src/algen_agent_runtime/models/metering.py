"""Charge for every model call the runtime dispatches.

A deployment that bills or rations model use passes a ``UsageMeter`` to
``build_container(usage_meter=...)`` (or ``ModelRouter(usage_meter=...)``). The router reserves
the most a call could cost before it reaches a provider and settles what the provider reports
afterwards, so steps, retries, fallbacks, repairs, streaming and compaction are all charged. A
call that fails is released without charge.

Each reservation carries a ``MeterCall``: whose call it is (tenant, user, run, agent), why it is
made (a step or compaction, which attempt) and which model is being tried. A multi-tenant meter
charges the right account from it and keeps it in the ticket to weight the settled tokens by
model.

A meter refuses a call by raising ``MeterRefusedError`` from ``reserve``. The router does not
try another candidate and the runtime does not retry; the run fails with the refusal message.
"""

from __future__ import annotations

import inspect
import sys
import warnings
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol, runtime_checkable

from algen_agent_runtime.types.contracts import TokenUsage

# Reserved output for a request without ``max_output_tokens``. The runtime always sets one for
# agent steps and compaction; this only covers direct router use.
DEFAULT_RESERVED_OUTPUT_TOKENS = 4_096

MeterPurpose = Literal["step", "compaction", "other"]


@dataclass(frozen=True, kw_only=True)
class MeterCall:
    """Who a metered model call is for, why it is made and which model is being tried.

    The runtime fills the run fields from the persisted run, so a resumed or recovered run
    reports the same tenant, user and run as before it paused. The router fills ``provider``,
    ``model`` and ``streaming`` for each candidate it tries, so a fallback reports the model
    actually called. A call made on the router directly, outside a run, has ``purpose="other"``
    and no run fields.

    ``metadata`` is a read-only copy of the run request's metadata, for an account id such as a
    workspace. The runtime never puts its values in events, audits or spans.
    """

    tenant_id: str | None = None
    user_id: str | None = None
    run_id: str | None = None
    parent_run_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    turn_id: str | None = None
    agent: str | None = None
    agent_version: str | None = None
    step_id: str | None = None
    purpose: MeterPurpose = "other"
    attempt: int = 0
    provider: str = ""
    model: str = ""
    streaming: bool = False
    metadata: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}), hash=False)

    def __post_init__(self) -> None:
        # Copy, so neither the caller's dict nor the meter can change the other's view.
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@runtime_checkable
class UsageMeter(Protocol):
    """Reserve, settle and release the token cost of each model call.

    ``settle`` and ``release`` receive only the ticket ``reserve`` returned. A meter that needs
    the call at settle time, for example to weight tokens by ``call.model`` or to charge
    ``call.tenant_id``, keeps the ``MeterCall`` inside its ticket.
    """

    async def reserve(
        self, *, max_input_tokens: int, max_output_tokens: int, call: MeterCall
    ) -> object:
        """Hold the most a call could cost and return a ticket for it.

        ``max_input_tokens`` is the router's estimate of the request sent to the selected
        model (``estimate_request_tokens``, times the router's ``reserve_input_margin``);
        ``max_output_tokens`` is the request's output cap;
        ``call`` says whose call it is and which model is being tried. Raise
        ``MeterRefusedError`` to refuse the call.

        A meter written for 0.1.0a14, whose ``reserve`` has no ``call`` parameter, still works:
        the router calls it without ``call`` and warns once. That form is removed in 0.2.
        """
        ...

    async def settle(self, ticket: object, usage: TokenUsage) -> None:
        """Charge a completed call what the provider reported it used."""
        ...

    async def release(self, ticket: object) -> None:
        """Drop the reservation of a call that failed or was cancelled, without charge."""
        ...


# Legacy meters already warned about, so a meter shared by several routers warns once.
_warned_meters: weakref.WeakSet[object] = weakref.WeakSet()
_warned_meter_ids: set[int] = set()


def reserve_accepts_call(meter: UsageMeter) -> bool:
    """Whether ``meter.reserve`` takes ``call``; warn once per meter for the a14 form.

    The router asks once, when it is built, never per call.
    """
    try:
        parameters = inspect.signature(meter.reserve).parameters.values()
    except (TypeError, ValueError):
        # Nothing to inspect (a C callable, an exotic proxy): assume the current protocol.
        return True
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "call"
            and parameter.kind
            in {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        for parameter in parameters
    ):
        return True
    _warn_legacy_meter(meter)
    return False


def _warn_legacy_meter(meter: object) -> None:
    try:
        if meter in _warned_meters:
            return
        _warned_meters.add(meter)
    except TypeError:
        # Not weak-referenceable or not hashable: fall back to its identity.
        if id(meter) in _warned_meter_ids:
            return
        _warned_meter_ids.add(id(meter))
    meter_type = type(meter)
    module_name = meter_type.__module__
    reserve = getattr(meter_type, "reserve", None)
    function = getattr(reserve, "__func__", reserve)
    code = getattr(inspect.unwrap(function), "__code__", None) if function is not None else None
    module = sys.modules.get(module_name)
    if code is not None:
        filename, lineno = code.co_filename, code.co_firstlineno
    else:
        filename, lineno = getattr(module, "__file__", None) or module_name, 1
    # Attribute the warning to the meter's own module, where the fix belongs, rather than to
    # whichever code built the router (often the runtime's own build_container).
    warnings.warn_explicit(
        f"{meter_type.__qualname__}.reserve() has no 'call' parameter; add "
        "'call: MeterCall' to it. The 0.1.0a14 reserve form is deprecated and will be removed "
        "in 0.2.",
        DeprecationWarning,
        filename=filename,
        lineno=lineno,
        module=module_name,
        module_globals=vars(module) if module is not None else None,
    )
