from __future__ import annotations

import io
import re

import pytest

from algen_agent_runtime import __version__
from algen_agent_runtime.banner import WIDTH, print_startup_banner, startup_banner
from algen_agent_runtime.config.settings import AppSettings

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _settings(**api: object) -> AppSettings:
    return AppSettings.model_validate(
        {
            "providers": {
                "ollama": {"type": "ollama", "base_url": "env://OLLAMA_URL", "default_model": "m"}
            },
            "agents": [
                {
                    "name": "outlook-assistant",
                    "version": "0.1.0",
                    "description": "a governed Outlook agent",
                    "system_instructions": "Help.",
                    "default_model": {"name": "d", "provider": "ollama", "model": "qwen3:4b"},
                    "enabled_tools": ["outlook.read_message", "outlook.send_email"],
                    "guardrail_policy": {"policies": ["secrets", "pii"]},
                }
            ],
            "api": api,
        }
    )


def _widths(text: str) -> set[int]:
    return {len(ANSI.sub("", line)) for line in text.splitlines()}


def test_the_banner_names_the_agent_and_what_it_serves() -> None:
    text = startup_banner(_settings(agent_ui_enabled=True))

    assert "✻ outlook-assistant · a governed Outlook agent" in text
    assert f"built with Algen Agent Runtime {__version__}" in text
    for row in (
        "agent   outlook-assistant 0.1.0",
        "model   ollama · qwen3:4b",
        "tools   2 enabled",
        "guards  2 active · secrets, pii",
        "api     http://127.0.0.1:8000",
        "ui      http://127.0.0.1:8000/agent-ui",
    ):
        assert row in text
    assert "env://" not in text and "OLLAMA_URL" not in text
    assert _widths(text) == {WIDTH + 2}
    assert "\x1b[" not in text


def test_an_application_names_itself_and_adds_rows() -> None:
    text = startup_banner(
        _settings(),
        title="ms-email-test",
        details={"tools": "2 enabled · every send needs approval", "mailbox": "fake " * 30},
        url="http://0.0.0.0:9000/",
    )

    assert "✻ ms-email-test · a governed Outlook agent" in text
    assert "tools    2 enabled · every send needs approval" in text
    assert "api      http://0.0.0.0:9000" in text
    assert "…" in text  # the long row is cut to fit the frame
    assert _widths(text) == {WIDTH + 2}


def test_colour_and_ascii_keep_the_frame_aligned() -> None:
    coloured = startup_banner(_settings(), color=True)
    assert "\x1b[38;2;" in coloured and _widths(coloured) == {WIDTH + 2}

    plain = startup_banner(_settings(), unicode=False)
    assert plain.isascii() and "█" not in plain and _widths(plain) == {WIDTH + 2}


def test_printing_follows_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()
    print_startup_banner(_settings(), file=stream)
    assert "\x1b[" not in stream.getvalue() and "█" in stream.getvalue()

    monkeypatch.setenv("FORCE_COLOR", "1")
    ascii_stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
    print_startup_banner(_settings(), file=ascii_stream)
    ascii_stream.seek(0)
    output = ascii_stream.read()
    assert "\x1b[38;2;" in output and "outlook-assistant" in output


def test_serve_prints_the_banner_only_when_asked_or_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from algen_agent_runtime import cli

    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    monkeypatch.setattr("algen_agent_runtime.api.app.create_app", lambda settings: None)
    monkeypatch.setenv("NO_COLOR", "1")

    cli.main(["serve", "--port", "8123"])
    assert "Algen Agent Runtime" not in capsys.readouterr().out  # captured output is no terminal

    cli.main(["serve", "--port", "8123", "--banner"])
    assert "api  http://127.0.0.1:8123" in capsys.readouterr().out
