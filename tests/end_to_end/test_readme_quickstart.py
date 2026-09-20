from __future__ import annotations

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.runtime.client import AlgenAgentRuntimeClient
from algen_agent_runtime.types.contracts import RunRequest


@pytest.mark.asyncio
async def test_readme_offline_quickstart() -> None:
    settings = AppSettings.model_validate(
        {
            "providers": {
                "mock": {"type": "mock", "default_model": "deterministic"}
            },
            "agents": [
                {
                    "name": "hello",
                    "version": "1.0.0",
                    "description": "Offline quickstart agent",
                    "system_instructions": "Answer concisely.",
                    "default_model": {
                        "name": "default",
                        "provider": "mock",
                        "model": "deterministic",
                    },
                }
            ],
        }
    )
    container = build_container(settings)
    try:
        result = await AlgenAgentRuntimeClient(container.runtime).run(
            RunRequest(
                agent="hello",
                input="Hello",
                tenant_id="quickstart",
                user_id="local-user",
            )
        )
    finally:
        await container.aclose()

    assert result.output == "Hello"
    assert result.error is None
