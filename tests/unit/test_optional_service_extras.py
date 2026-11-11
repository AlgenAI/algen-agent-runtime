"""The HTTP server and the OTLP exporter are optional extras (#115)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from algen_agent_runtime.config.settings import TelemetrySettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.extras import MissingExtraError

# Make the server and exporter packages unimportable in a fresh interpreter.
_BLOCK = (
    "import sys\n"
    "class Block:\n"
    "    def find_spec(self, name, path=None, target=None):\n"
    "        if name.split('.')[0] in {'fastapi', 'uvicorn', 'starlette'} or name.startswith(\n"
    "            'opentelemetry.exporter'\n"
    "        ):\n"
    "            raise ModuleNotFoundError(f'No module named {name!r}', name=name)\n"
    "sys.meta_path.insert(0, Block())\n"
)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK + code], capture_output=True, text=True, check=False
    )


def test_the_library_runs_without_the_server_or_exporter() -> None:
    result = _run(
        "import asyncio\n"
        "from algen_agent_runtime.orchestration.container import build_container\n"
        "from algen_agent_runtime.config.settings import AppSettings\n"
        "from algen_agent_runtime.observability.setup import configure_telemetry\n"
        "from algen_agent_runtime.config.settings import TelemetrySettings\n"
        "build_container(AppSettings())\n"
        "configure_telemetry(TelemetrySettings())\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_the_api_names_the_server_extra() -> None:
    result = _run(
        "from algen_agent_runtime.extras import MissingExtraError\n"
        "try:\n"
        "    import algen_agent_runtime.api.app\n"
        "except MissingExtraError as exc:\n"
        "    print(exc)\n"
    )
    assert result.returncode == 0, result.stderr
    assert "pip install 'algen-agent-runtime[server]'" in result.stdout


def test_serve_without_the_server_extra_exits_with_the_install_command() -> None:
    result = _run("from algen_agent_runtime.cli import main\nmain(['serve'])\n")

    assert result.returncode == 1
    assert "pip install 'algen-agent-runtime[server]'" in result.stderr
    assert "Traceback" not in result.stderr


def test_an_otlp_endpoint_without_the_exporter_names_the_otlp_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algen_agent_runtime.observability.setup import configure_telemetry

    monkeypatch.setitem(sys.modules, "opentelemetry.exporter.otlp.proto.http.trace_exporter", None)

    with pytest.raises(MissingExtraError, match=r"algen-agent-runtime\[otlp\]") as caught:
        configure_telemetry(TelemetrySettings(enabled=True, otlp_endpoint="http://collector:4318"))

    assert isinstance(caught.value, ConfigurationError)
    assert isinstance(caught.value, ImportError)
