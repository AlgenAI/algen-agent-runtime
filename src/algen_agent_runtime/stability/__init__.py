"""Which public interfaces are experimental (since ``0.2.0b5``).

An experimental interface is documented and usable but outside the compatibility promise: it can
change or go away in any minor release, with an upgrade note but without a deprecation window
(``docs/api-stability.md``, "Experimental interfaces"). Everything else in the public surface is
stable.

The marker is for people and tools, never for behaviour: marking an interface emits no warning
and changes nothing at run time.

- A class or function is marked with :func:`experimental`; its docstring starts with
  ``Experimental:``.
- A whole package is marked with a module-level ``__experimental__ = "<reason>"``.
- A configuration field is marked in the settings JSON schema
  (``AppSettings.model_json_schema()``) with ``"stability": "experimental"``; the fields under it
  are experimental too.

``scripts/api_surface.py`` reads these markers, so ``docs/api/surface.json`` records each
experimental export with ``"stability": "experimental"`` and lists every experimental entry under
its ``experimental`` key.

    >>> from algen_agent_runtime.frameworks import AutoGenAdapter, LangGraphAdapter
    >>> from algen_agent_runtime.stability import is_experimental
    >>> is_experimental(AutoGenAdapter), is_experimental(LangGraphAdapter)
    (True, False)
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType
from typing import Any, TypeVar

__all__ = [
    "EXPERIMENTAL",
    "EXPERIMENTAL_ATTRIBUTE",
    "EXPERIMENTAL_FIELD",
    "experimental",
    "experimental_reason",
    "is_experimental",
]

#: The stability value of an experimental interface, in ``surface.json`` and the settings schema.
EXPERIMENTAL = "experimental"
#: The attribute that marks a class, function or package as experimental; its value is the reason.
EXPERIMENTAL_ATTRIBUTE = "__experimental__"
#: ``json_schema_extra`` for an experimental configuration field:
#: ``Field(..., json_schema_extra=EXPERIMENTAL_FIELD)``.
EXPERIMENTAL_FIELD: dict[str, Any] = {"stability": EXPERIMENTAL}

_Marked = TypeVar("_Marked")


def experimental(reason: str) -> Callable[[_Marked], _Marked]:
    """Mark a class or function as experimental, giving the reason; it is returned unchanged.

    Only the marker is added: no wrapper, no warning, no change in behaviour.
    """
    if not reason.strip():
        raise ValueError("an experimental interface needs a reason")

    def mark(value: _Marked) -> _Marked:
        setattr(value, EXPERIMENTAL_ATTRIBUTE, reason)
        return value

    return mark


def experimental_reason(value: object) -> str | None:
    """Why ``value`` (a class, function or module) is experimental, or ``None`` when it is stable.

    A class is experimental when it is marked itself (a subclass of an experimental class is not,
    unless marked too) or when the package that defines it is marked; so is a function.
    """
    if isinstance(value, ModuleType):
        return _package_reason(value.__name__)
    if isinstance(value, type):
        reason = vars(value).get(EXPERIMENTAL_ATTRIBUTE)
    else:
        reason = getattr(value, EXPERIMENTAL_ATTRIBUTE, None)
    if isinstance(reason, str):
        return reason
    module = getattr(value, "__module__", None)
    return _package_reason(module) if isinstance(module, str) else None


def is_experimental(value: object) -> bool:
    """Whether ``value`` (a class, function or module) is experimental."""
    return experimental_reason(value) is not None


def _package_reason(module_name: str) -> str | None:
    """The reason the module or one of the packages that contain it gives, if any is marked."""
    parts = module_name.split(".")
    for end in range(len(parts), 0, -1):
        module = sys.modules.get(".".join(parts[:end]))
        reason = getattr(module, EXPERIMENTAL_ATTRIBUTE, None) if module else None
        if isinstance(reason, str):
            return reason
    return None
