"""Check a project the way Studio will: its settings, components and code Studio cannot run."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from algen_agent_runtime.config.settings import load_settings
from algen_agent_runtime.exceptions.errors import ConfigurationError, ConflictError
from algen_agent_runtime.orchestration.container import SUPPORTED_PROVIDER_TYPES
from algen_agent_runtime.orchestration.describe import ComponentInventory, describe_components
from algen_agent_runtime.orchestration.validation import (
    AvailableComponents,
    closest,
    unknown_agent_components,
)

Severity = Literal["error", "warning"]

CONSTRUCTOR_COMPONENTS = ("additional_tools", "mcp_servers", "framework_adapters")
SKIPPED_DIRECTORIES = frozenset(
    {"node_modules", "__pycache__", "build", "dist", "venv", "site-packages", "tests", "test"}
)


class CheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    severity: Severity
    code: str
    message: str
    location: str | None = None
    agent: str | None = None
    field: str | None = None
    name: str | None = None
    suggestion: str | None = None


class CheckReport(BaseModel):
    """What ``algen-agent-runtime check`` found. ``ok`` is false when any issue is an error."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: bool
    config: tuple[str, ...]
    issues: tuple[CheckIssue, ...] = ()
    inventory: ComponentInventory | None = None

    @property
    def errors(self) -> tuple[CheckIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[CheckIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")


def check_project(config_paths: Sequence[Path], *, project_root: Path | None = None) -> CheckReport:
    """Validate settings, describe components, check every name agents use, and scan the
    project's own Python for behaviour Studio will not run.

    Nothing is executed beyond importing declared plugins and running their ``register``.
    """
    config = tuple(str(path) for path in config_paths)
    issues = list(scan_project_code(project_root)) if project_root is not None else []
    try:
        settings = load_settings(tuple(config_paths), ignore_extensions=True)
    except ConfigurationError as exc:
        return _report(config, [_error("CONFIGURATION_ERROR", str(exc)), *issues])
    try:
        inventory = describe_components(settings)
    except ConflictError as exc:
        return _report(config, [_error("COMPONENT_CONFLICT", str(exc)), *issues])
    except ConfigurationError as exc:
        return _report(config, [_error("PLUGIN_ERROR", str(exc)), *issues])

    found: list[CheckIssue] = [
        CheckIssue(
            severity="error",
            code="UNKNOWN_COMPONENT",
            message=problem.message,
            agent=problem.agent,
            field=problem.field,
            name=problem.name,
            suggestion=problem.suggestion,
        )
        for problem in unknown_agent_components(
            settings.agents, AvailableComponents.from_inventory(inventory)
        )
    ]
    for name, provider in settings.providers.items():
        if provider.type not in SUPPORTED_PROVIDER_TYPES:
            suggestion = closest(provider.type, SUPPORTED_PROVIDER_TYPES)
            found.append(
                CheckIssue(
                    severity="error",
                    code="UNSUPPORTED_PROVIDER_TYPE",
                    message=f"provider {name!r} has unsupported type {provider.type!r}"
                    + (f"; did you mean {suggestion!r}?" if suggestion else ""),
                    field=f"providers.{name}.type",
                    name=provider.type,
                    suggestion=suggestion,
                )
            )
    return _report(config, [*found, *issues], inventory)


def _error(code: str, message: str) -> CheckIssue:
    return CheckIssue(severity="error", code=code, message=message)


def _report(
    config: tuple[str, ...],
    issues: list[CheckIssue],
    inventory: ComponentInventory | None = None,
) -> CheckReport:
    return CheckReport(
        ok=not any(issue.severity == "error" for issue in issues),
        config=config,
        issues=tuple(issues),
        inventory=inventory,
    )


def _python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") or part in SKIPPED_DIRECTORIES for part in parts[:-1]):
            continue
        name = path.name
        if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
            continue
        yield path


def scan_project_code(root: Path) -> Iterator[CheckIssue]:
    """Warnings for project code Studio's runner will not execute or cannot see.

    Test files and directories, virtual environments and hidden directories are skipped.
    """
    for path in _python_files(root):
        location = str(path.relative_to(root))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError) as exc:
            yield CheckIssue(
                severity="warning",
                code="UNREADABLE_SOURCE",
                message=f"could not parse {location}: {exc}",
                location=location,
            )
            continue
        yield from _CodeScanner(location).scan(tree)


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


class _CodeScanner(ast.NodeVisitor):
    def __init__(self, location: str) -> None:
        self._location = location
        self._functions: list[str] = []
        self._env_reads: list[int] = []
        self._issues: list[CheckIssue] = []

    def scan(self, tree: ast.AST) -> list[CheckIssue]:
        self.visit(tree)
        if self._env_reads:
            lines = ", ".join(str(line) for line in self._env_reads[:10])
            more = "" if len(self._env_reads) <= 10 else ", ..."
            self._issues.append(
                CheckIssue(
                    severity="warning",
                    code="DIRECT_ENV_READ",
                    message=(
                        f"{self._location} reads environment variables directly "
                        f"{len(self._env_reads)} time(s) (lines {lines}{more}); Studio deploys "
                        "ask only for env:// references and PluginManifest.env"
                    ),
                    location=f"{self._location}:{self._env_reads[0]}",
                )
            )
        return self._issues

    def _warn(self, code: str, message: str, node: ast.AST) -> None:
        line = getattr(node, "lineno", None)
        self._issues.append(
            CheckIssue(
                severity="warning",
                code=code,
                message=message,
                location=f"{self._location}:{line}" if line else self._location,
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in {"environ", "getenv", "environb"}
        ):
            self._env_reads.append(node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os" and any(
            alias.name in {"environ", "getenv", "environb"} for alias in node.names
        ):
            self._env_reads.append(node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        in_plugin = "register" in self._functions
        if name == "build_container":
            self._warn(
                "OWN_CONTAINER",
                "builds its own container; Studio builds the container from the settings and "
                "does not run this entry point, its server or its workers",
                node,
            )
            passed = [
                keyword.arg for keyword in node.keywords if keyword.arg in CONSTRUCTOR_COMPONENTS
            ]
            if passed:
                self._warn(
                    "CONSTRUCTOR_COMPONENTS",
                    f"passes {', '.join(passed)} to build_container; Studio cannot see "
                    "constructor arguments, so register them from a declared plugin",
                    node,
                )
        elif name == "AgentDefinition":
            self._warn(
                "CODE_DEFINED_AGENT",
                "defines an agent in code; Studio sees only agents in the settings",
                node,
            )
        elif not in_plugin and any(
            isinstance(argument, ast.Attribute) and argument.attr == "tools"
            for argument in node.args
        ):
            self._warn(
                "POST_BUILD_REGISTRATION",
                "passes a built container's tool registry to be filled in code; Studio cannot "
                "see those tools, so register them from a declared plugin",
                node,
            )
        elif not in_plugin and name in {"register", "register_provider", "register_handler"}:
            target = node.func.value if isinstance(node.func, ast.Attribute) else None
            registry = target.attr if isinstance(target, ast.Attribute) else None
            if name in {"register_provider", "register_handler"} or registry in {
                "tools",
                "planners",
                "contexts",
                "verifiers",
                "composers",
                "policies",
                "retrievers",
                "semantics",
            }:
                self._warn(
                    "POST_BUILD_REGISTRATION",
                    "registers a component on a built container or router; Studio cannot see "
                    "it, so register it from a declared plugin",
                    node,
                )
        self.generic_visit(node)
