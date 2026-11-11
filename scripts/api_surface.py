"""The Runtime's public surface, written to docs/api/surface.json and compared in CI.

The surface is what docs/api-stability.md promises to keep compatible:

- names exported from ``algen_agent_runtime`` and from every subpackage's ``__all__``, with the
  signature of each function and class and the fields of each Pydantic model;
- the HTTP routes of the API app (method and path);
- every configuration field (``AppSettings``, dotted path and type);
- the exception classes and their bases;
- event and audit identifiers the Runtime emits, and the codes ``check`` reports;
- the extension-point protocols (``typing.Protocol`` classes) and their methods;
- the SQL migrations.

Usage:
    python scripts/api_surface.py           # print the current surface
    python scripts/api_surface.py --update  # rewrite docs/api/surface.json
tests/unit/test_api_surface.py fails when the code and docs/api/surface.json differ.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import pkgutil
import re
import typing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "algen_agent_runtime"
BASELINE = ROOT / "docs" / "api" / "surface.json"
EVENT = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
CODE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
EMITTERS = {"emit", "_emit", "audit", "_audit", "publish", "_publish", "record_event"}


ADDRESS = re.compile(r" at 0x[0-9a-f]+")
FORWARD = re.compile(r"ForwardRef\('([^']+)'\)")
CLASS = re.compile(r"<class '(?:[\w.]+\.)?(\w+)'>")
QUALIFIED = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.)+([A-Za-z_]\w*)")


def _type_text(annotation: Any) -> str:
    """A type as plain names, the same whether a forward reference has been resolved yet (that
    depends on what was imported first) or not."""
    if inspect.isclass(annotation) and not typing.get_args(annotation):
        return annotation.__name__
    text = FORWARD.sub(r"\1", str(annotation))
    return QUALIFIED.sub(r"\1", CLASS.sub(r"\1", text))


def _signature(value: Any) -> str:
    try:
        # A default such as <function _text at 0x…> differs per process: keep only its name.
        return ADDRESS.sub("", str(inspect.signature(value)))
    except (TypeError, ValueError):
        return "(…)"


def _describe(value: Any) -> Any:
    from pydantic import BaseModel

    if inspect.isclass(value):
        if issubclass(value, BaseModel):
            return {
                "model": {
                    name: _type_text(field.annotation)
                    for name, field in sorted(value.model_fields.items())
                }
            }
        if typing.Protocol in getattr(value, "__mro__", ()) or getattr(
            value, "_is_protocol", False
        ):
            return {"protocol": _protocol_methods(value)}
        methods = {
            name: _signature(member)
            for name, member in sorted(vars(value).items())
            if not name.startswith("_")
            and (inspect.isfunction(member) or isinstance(member, (staticmethod, classmethod)))
        }
        return (
            {"class": _signature(value), "methods": methods}
            if methods
            else {"class": _signature(value)}
        )
    if inspect.isfunction(value) or inspect.isbuiltin(value) or inspect.ismethod(value):
        return {"function": _signature(value)}
    if typing.get_origin(value) is not None:
        # A type alias such as Callable[[...], ...]: its text, not a call signature.
        return {"alias": str(value).replace("typing.", "").replace("collections.abc.", "")}
    if callable(value):
        return {"function": _signature(value)}
    return {"value": type(value).__name__}


def _protocol_methods(cls: type) -> dict[str, str]:
    return {
        name: _signature(member)
        for name, member in sorted(vars(cls).items())
        if not name.startswith("_") and callable(member)
    }


def exports() -> dict[str, dict[str, Any]]:
    import algen_agent_runtime

    modules = {"algen_agent_runtime": algen_agent_runtime}
    for info in pkgutil.walk_packages(algen_agent_runtime.__path__, "algen_agent_runtime."):
        if not info.ispkg:
            continue
        try:
            modules[info.name] = importlib.import_module(info.name)
        except ImportError:
            continue  # a package behind an optional extra
    surface: dict[str, dict[str, Any]] = {}
    for name, module in sorted(modules.items()):
        names = getattr(module, "__all__", None)
        if names is None:
            continue
        surface[name] = {item: _describe(getattr(module, item)) for item in sorted(names)}
    return surface


def routes() -> list[str]:
    from algen_agent_runtime.api.app import create_app

    found = set()
    for route in create_app().routes:
        for method in sorted(getattr(route, "methods", None) or {"WS"}):
            if method != "HEAD":
                found.add(f"{method} {route.path}")
    return sorted(found)


def configuration() -> dict[str, str]:
    from algen_agent_runtime.config.settings import AppSettings

    schema = AppSettings.model_json_schema()
    definitions = schema.get("$defs", {})
    fields: dict[str, str] = {}

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            node = definitions[node["$ref"].rsplit("/", 1)[1]]
        return node

    def kind(node: dict[str, Any]) -> str:
        node = resolve(node)
        if "anyOf" in node:
            return " | ".join(kind(option) for option in node["anyOf"])
        if "enum" in node:
            return "enum[" + ", ".join(json.dumps(item) for item in node["enum"]) + "]"
        if node.get("type") == "array":
            return f"list[{kind(node.get('items', {}))}]"
        return str(node.get("type") or node.get("title") or "any")

    def walk(node: dict[str, Any], prefix: str, seen: frozenset[str]) -> None:
        node = resolve(node)
        if node.get("type") == "array":
            walk(node.get("items", {}), f"{prefix}[]", seen)
            return
        for option in node.get("anyOf", []):
            walk(option, prefix, seen)
        properties = node.get("properties")
        if not properties:
            return
        title = str(node.get("title"))
        if title in seen:
            return
        for name, child in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            fields[path] = kind(child)
            walk(child, path, seen | {title})

    walk(schema, "", frozenset())
    return dict(sorted(fields.items()))


def errors() -> dict[str, list[str]]:
    from algen_agent_runtime.exceptions import errors as module

    return {
        name: [base.__name__ for base in cls.__bases__]
        for name, cls in sorted(vars(module).items())
        if inspect.isclass(cls)
        and issubclass(cls, BaseException)
        and cls.__module__ == module.__name__
    }


def protocols() -> dict[str, dict[str, str]]:
    """Every typing.Protocol class in the package, exported or not: the extension points."""
    found: dict[str, dict[str, str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and not node.name.startswith("_")
            and any(
                (isinstance(base, ast.Name) and base.id == "Protocol")
                or (isinstance(base, ast.Subscript) and getattr(base.value, "id", "") == "Protocol")
                for base in node.bases
            )
        ]
        if not names:
            continue
        module_name = ".".join(path.relative_to(SOURCE.parent).with_suffix("").parts)
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue  # behind an optional extra
        for name in names:
            found[f"{module_name}.{name}"] = _protocol_methods(getattr(module, name))
    return found


def identifiers() -> tuple[list[str], list[str]]:
    """Event and audit identifiers passed to an emitter, and the codes check reports."""
    events: set[str] = set()
    codes: set[str] = set()
    for path in SOURCE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            function = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if function in EMITTERS:
                for argument in node.args[:3]:
                    if (
                        isinstance(argument, ast.Constant)
                        and isinstance(argument.value, str)
                        and EVENT.match(argument.value)
                    ):
                        events.add(argument.value)
            for keyword in node.keywords:
                if (
                    keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    and CODE.match(keyword.value.value)
                ):
                    codes.add(keyword.value.value)
    return sorted(events), sorted(codes)


def surface() -> dict[str, Any]:
    events, codes = identifiers()
    return {
        "exports": exports(),
        "http_routes": routes(),
        "configuration": configuration(),
        "errors": errors(),
        "protocols": protocols(),
        "events": events,
        "check_codes": codes,
        "migrations": sorted(
            path.name for path in (SOURCE / "persistence" / "migrations").glob("*.sql")
        ),
    }


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--update", action="store_true", help="rewrite docs/api/surface.json")
    args = parser.parse_args()
    text = render(surface())
    if args.update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
