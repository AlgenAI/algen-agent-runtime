"""Errors for features whose optional dependencies are not installed."""

from __future__ import annotations

from algen_agent_runtime.exceptions.errors import ConfigurationError


class MissingExtraError(ConfigurationError, ModuleNotFoundError):
    """A feature needs an extra that is not installed; the message names the install command.

    It is a ``ModuleNotFoundError`` too, so code that probes for an optional feature with
    ``except ImportError`` keeps working.
    """

    def __init__(self, feature: str, extra: str, *, missing: str | None = None) -> None:
        detail = f" ({missing} is not installed)" if missing else ""
        super().__init__(
            f"{feature} needs the '{extra}' extra{detail}: "
            f"pip install 'algen-agent-runtime[{extra}]'"
        )
        self.extra = extra
        if missing:
            self.name = missing
