import re

from algen_agent_runtime import __version__
from algen_agent_runtime.api.app import create_app


def test_fastapi_reports_package_version() -> None:
    assert create_app().version == __version__


def test_package_version_is_private_testpypi_candidate() -> None:
    assert re.match(r"^0\.1\.\d+(?:[abrc]\d+)?$", __version__)


# Extras name a technology (postgres, mcp, documents), never a product or domain. Product
# dependency sets belong to the product's own package (a domain pack), not to the core.
DOMAIN_TERMS = ("hiring", "talk-to-data", "text-to-sql", "hr", "hrms", "payroll", "recruit")


def test_the_core_has_no_domain_extras() -> None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    extras = tomllib.loads(pyproject.read_text())["project"]["optional-dependencies"]

    def is_domain(name: str) -> bool:
        words = {name, *name.split("-")}
        return any(term in words or ("-" in term and term in name) for term in DOMAIN_TERMS)

    domain = sorted(name for name in extras if is_domain(name))
    assert domain == [], f"domain extras belong in a domain pack: {domain}"
