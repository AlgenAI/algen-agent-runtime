"""Run a hosted-provider example offline, with the deterministic mock provider.

Examples that call OpenAI, Mistral or Anthropic take an ``--offline`` flag. It loads the same
``agent.yaml`` with the named providers swapped for the built-in ``mock`` provider, keeping their
registration names, so every agent, model profile and allowlist is unchanged. The example then
scripts the mock's replies with ``MockModelProvider.queue``. Nothing leaves the process and no
API key is read.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from algen_agent_runtime.config.settings import AppSettings, load_settings
from algen_agent_runtime.models.providers.mock import MockModelProvider
from algen_agent_runtime.orchestration.container import Container
from algen_agent_runtime.types.contracts import ModelResponse


def offline_settings(files: Iterable[str | Path], providers: Iterable[str]) -> AppSettings:
    """Load ``files`` with each named provider replaced by the mock provider."""
    mock: dict[str, Any] = {"type": "mock", "api_key": None, "base_url": None}
    return load_settings(
        tuple(files), deployment_overrides={"providers": {name: mock for name in providers}}
    )


def script(container: Container, provider: str, *replies: str | ModelResponse) -> None:
    """Queue the mock provider's next replies (text answers or ``tool_call_response`` calls)."""
    model = container.runtime.router.provider(provider)
    if not isinstance(model, MockModelProvider):
        raise TypeError(f"provider {provider!r} is not the mock provider; pass --offline")
    model.queue(*replies)


def require_key(variable: str) -> None:
    """Exit with a hint when a hosted provider's key is missing, instead of a provider error."""
    if not os.getenv(variable):
        sys.exit(f"{variable} is not set. Export it, or run this example with --offline.")
