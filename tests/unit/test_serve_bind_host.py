"""``serve --host`` cannot publish development-header authentication beyond loopback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


def _serve(monkeypatch: pytest.MonkeyPatch, *argv: str) -> list[dict[str, Any]]:
    from algen_agent_runtime import cli

    served: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: served.append(kwargs))
    monkeypatch.setattr("algen_agent_runtime.api.app.create_app", lambda settings: settings)
    monkeypatch.delenv("ALGEN_AGENT_RUNTIME_CONFIG", raising=False)
    cli.main(["serve", *argv])
    return served


def test_a_public_host_with_development_headers_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as exited:
        _serve(monkeypatch, "--host", "0.0.0.0")

    message = str(exited.value.code)
    assert message.startswith("error: invalid API settings for serve")
    assert "development_headers" in message
    assert "allow_insecure_development_auth" in message


def test_a_loopback_host_override_is_served(monkeypatch: pytest.MonkeyPatch) -> None:
    served = _serve(monkeypatch, "--host", "::1", "--port", "8123")

    assert [(kwargs["host"], kwargs["port"]) for kwargs in served] == [("::1", 8123)]


def test_the_explicit_insecure_override_still_allows_a_public_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "agent.yaml"
    config.write_text("api:\n  allow_insecure_development_auth: true\n")

    served = _serve(monkeypatch, "--config", str(config), "--host", "0.0.0.0")

    assert [kwargs["host"] for kwargs in served] == ["0.0.0.0"]


def test_an_invalid_port_override_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="invalid API settings"):
        _serve(monkeypatch, "--port", "70000")


def test_create_app_refuses_settings_that_skipped_validation() -> None:
    from algen_agent_runtime.api.app import create_app

    settings = AppSettings()
    unchecked = settings.model_copy(
        update={"api": settings.api.model_copy(update={"host": "0.0.0.0"})}
    )

    with pytest.raises(ConfigurationError, match="development_headers"):
        create_app(unchecked)
