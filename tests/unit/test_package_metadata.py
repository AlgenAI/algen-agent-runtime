from algen_agent_runtime import __version__
from algen_agent_runtime.api.app import create_app


def test_fastapi_reports_package_version() -> None:
    assert create_app().version == __version__


def test_package_version_is_private_testpypi_candidate() -> None:
    assert __version__ == "0.1.0a1"
