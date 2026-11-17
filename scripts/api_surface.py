"""The Runtime's public surface, written to docs/api/surface.json and compared in CI.

The surface is what docs/api-stability.md promises to keep compatible:

- names exported from ``algen_agent_runtime`` and from every subpackage's ``__all__``, with the
  signature of each function and class and the fields of each Pydantic model;
- the HTTP routes of the API app (method and path);
- every configuration field (``AppSettings``, dotted path and type);
- the exception classes and their bases;
- event and audit identifiers the Runtime emits, and the codes ``check`` reports;
- the payload shapes of run, conversation and workflow events (``event_payloads``): each
  envelope's fields, the keys each event type's ``data`` can carry, and the server-sent-event
  frames of the event streams;
- the extension-point protocols (``typing.Protocol`` classes) and their methods;
- the SQL migrations;
- which of those entries are experimental (``algen_agent_runtime.stability``): an experimental
  export carries ``"stability": "experimental"``, and ``experimental`` lists every experimental
  export, protocol and configuration field.

Usage:
    python scripts/api_surface.py            # print the current surface
    python scripts/api_surface.py --update   # rewrite docs/api/surface.json
    python scripts/api_surface.py --changes  # how the code differs from docs/api/surface.json
tests/unit/test_api_surface.py fails when the code and docs/api/surface.json differ, and groups
the differences with ``changes``: removed, changed, added, and experimental (a change to an
experimental entry needs a CHANGELOG line but no deprecation window).
"""

from __future__ import annotations

import argparse
import ast
import functools
import importlib
import inspect
import json
import pkgutil
import re
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "algen_agent_runtime"
BASELINE = ROOT / "docs" / "api" / "surface.json"
EVENT = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
CODE = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
EMITTERS = {"emit", "_emit", "audit", "_audit", "publish", "_publish", "record_event"}
#: The emitters of event payloads, and the event family of each package that calls them.
PAYLOAD_EMITTERS = {"emit", "_emit"}
EVENT_FAMILIES = {"runtime": "run", "conversations": "conversation", "workflows": "workflow"}
#: In a payload's keys: the payload can carry keys this script cannot name (a caller's mapping).
MORE = "*"
EXPERIMENTAL = "experimental"
#: How ``changes`` groups differences, and what each group needs.
CHANGE_GROUPS = {
    "removed": "breaking: needs an upgrade note and restarts the stability window",
    "changed": "check compatibility; an incompatible change is breaking",
    "added": "additive: a CHANGELOG line",
    "experimental": "allowed: a CHANGELOG line (and an upgrade note), no deprecation window",
}


ADDRESS = re.compile(r" at 0x[0-9a-f]+")
FORWARD = re.compile(r"ForwardRef\('([^']+)'\)")
CLASS = re.compile(r"<class '(?:[\w.]+\.)?(\w+)'>")
QUALIFIED = re.compile(r"\b(?:[a-z_][a-z0-9_]*\.)+([A-Za-z_]\w*)")

_Function = ast.FunctionDef | ast.AsyncFunctionDef


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


def _is_experimental(value: Any) -> bool:
    from algen_agent_runtime.stability import is_experimental

    try:
        return is_experimental(value)
    except Exception:  # an export whose attributes cannot be read is not marked
        return False


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
        package_experimental = _is_experimental(module)
        entries: dict[str, Any] = {}
        for item in sorted(names):
            value = getattr(module, item)
            entry = _describe(value)
            if package_experimental or _is_experimental(value):
                entry["stability"] = EXPERIMENTAL
            entries[item] = entry
        surface[name] = entries
    return surface


def routes() -> list[str]:
    from algen_agent_runtime.api.app import create_app

    found = set()
    for route in create_app().routes:
        for method in sorted(getattr(route, "methods", None) or {"WS"}):
            if method != "HEAD":
                found.add(f"{method} {route.path}")
    return sorted(found)


def _configuration() -> tuple[dict[str, str], list[str]]:
    """Every configuration field with its type, and the fields marked experimental in the
    settings schema (``"stability": "experimental"``) or under one that is."""
    from algen_agent_runtime.config.settings import AppSettings

    schema = AppSettings.model_json_schema()
    definitions = schema.get("$defs", {})
    fields: dict[str, str] = {}
    experimental: set[str] = set()

    def resolve(node: dict[str, Any]) -> dict[str, Any]:
        while "$ref" in node:
            node = definitions[node["$ref"].rsplit("/", 1)[1]]
        return node

    def marked(node: dict[str, Any]) -> bool:
        return node.get("stability") == EXPERIMENTAL or any(
            marked(option) for option in node.get("allOf", [])
        )

    def kind(node: dict[str, Any]) -> str:
        node = resolve(node)
        if "anyOf" in node:
            return " | ".join(kind(option) for option in node["anyOf"])
        if "enum" in node:
            return "enum[" + ", ".join(json.dumps(item) for item in node["enum"]) + "]"
        if node.get("type") == "array":
            return f"list[{kind(node.get('items', {}))}]"
        return str(node.get("type") or node.get("title") or "any")

    def walk(node: dict[str, Any], prefix: str, seen: frozenset[str], flagged: bool) -> None:
        node = resolve(node)
        if node.get("type") == "array":
            walk(node.get("items", {}), f"{prefix}[]", seen, flagged)
            return
        for option in node.get("anyOf", []):
            walk(option, prefix, seen, flagged)
        properties = node.get("properties")
        if not properties:
            return
        title = str(node.get("title"))
        if title in seen:
            return
        for name, child in properties.items():
            path = f"{prefix}.{name}" if prefix else name
            fields[path] = kind(child)
            child_flagged = flagged or marked(child)
            if child_flagged:
                experimental.add(path)
            walk(child, path, seen | {title}, child_flagged)

    walk(schema, "", frozenset(), False)
    return dict(sorted(fields.items())), sorted(experimental)


def configuration() -> dict[str, str]:
    return _configuration()[0]


def errors() -> dict[str, list[str]]:
    from algen_agent_runtime.exceptions import errors as module

    return {
        name: [base.__name__ for base in cls.__bases__]
        for name, cls in sorted(vars(module).items())
        if inspect.isclass(cls)
        and issubclass(cls, BaseException)
        and cls.__module__ == module.__name__
    }


def _protocol_classes() -> Iterator[tuple[str, type]]:
    """Every typing.Protocol class in the package, exported or not, by qualified name."""
    for path, tree in _trees():
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
            yield f"{module_name}.{name}", getattr(module, name)


def protocols() -> dict[str, dict[str, str]]:
    """Every typing.Protocol class in the package, exported or not: the extension points."""
    return {name: _protocol_methods(cls) for name, cls in _protocol_classes()}


@functools.cache
def _trees() -> tuple[tuple[Path, ast.Module], ...]:
    return tuple(
        (path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(SOURCE.rglob("*.py"))
    )


@functools.cache
def _constants() -> dict[str, str]:
    """Module-level string constants named in capitals (``DELTA_EVENT = "..."``), by name; a name
    that two modules give different values is left out."""
    values: dict[str, set[str]] = {}
    for _, tree in _trees():
        for node in tree.body:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if (
                isinstance(target, ast.Name)
                and target.id.lstrip("_").isupper()
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                values.setdefault(target.id, set()).add(value.value)
    return {name: next(iter(found)) for name, found in values.items() if len(found) == 1}


def _event_names(node: ast.expr | None) -> list[str]:
    """The event identifiers an argument names: a literal, a module constant, or either branch
    of a conditional expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value] if EVENT.match(node.value) else []
    if isinstance(node, ast.Name):
        value = _constants().get(node.id)
        return [value] if value and EVENT.match(value) else []
    if isinstance(node, ast.IfExp):
        return _event_names(node.body) + _event_names(node.orelse)
    return []


def _function_name(call: ast.Call) -> str:
    return call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", "")


def identifiers() -> tuple[list[str], list[str]]:
    """Event and audit identifiers passed to an emitter, and the codes check reports."""
    events: set[str] = set()
    codes: set[str] = set()
    for _, tree in _trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _function_name(node) in EMITTERS:
                for argument in node.args[:3]:
                    events.update(_event_names(argument))
            for keyword in node.keywords:
                if (
                    keyword.arg == "code"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                    and CODE.match(keyword.value.value)
                ):
                    codes.add(keyword.value.value)
    return sorted(events), sorted(codes)


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function's body, without those of the functions and classes it defines."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _parameters(function: _Function) -> list[str]:
    names = [argument.arg for argument in (*function.args.posonlyargs, *function.args.args)]
    return names[1:] if names and names[0] in {"self", "cls"} else names


class _PayloadKeys:
    """The top-level keys a payload expression can carry, read from the source (statically)."""

    def __init__(self, tree: ast.Module, siblings: list[ast.Module]) -> None:
        self.functions = self._functions([tree])
        self.sibling_functions = self._functions(siblings)
        self.parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        self._resolving: set[tuple[int, str]] = set()

    @staticmethod
    def _functions(trees: list[ast.Module]) -> dict[str, list[_Function]]:
        found: dict[str, list[_Function]] = {}
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.setdefault(node.name, []).append(node)
        return found

    def enclosing(self, node: ast.AST) -> _Function | None:
        current = self.parents.get(node)
        while current is not None and not isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            current = self.parents.get(current)
        return current

    def emitter(self, name: str) -> _Function | None:
        """The emitter's definition, in this module or another one of its package."""
        for function in [*self.functions.get(name, []), *self.sibling_functions.get(name, [])]:
            if "event_type" in _parameters(function):
                return function
        return None

    def keys(self, node: ast.expr | None, scope: _Function | None, depth: int = 0) -> set[str]:
        if node is None or (isinstance(node, ast.Constant) and node.value is None):
            return set()
        if depth > 12:
            return {MORE}
        if isinstance(node, ast.Dict):
            found: set[str] = set()
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    found |= self.keys(value, scope, depth + 1)
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.add(key.value)
                else:
                    found.add(MORE)
            return found
        if isinstance(node, ast.IfExp):
            return self.keys(node.body, scope, depth + 1) | self.keys(node.orelse, scope, depth + 1)
        if isinstance(node, ast.BoolOp):
            return set().union(*(self.keys(value, scope, depth + 1) for value in node.values))
        if isinstance(node, ast.Name):
            return self._name(node.id, scope, depth)
        if isinstance(node, ast.Call):
            return self._call(node, scope, depth)
        return {MORE}

    def _call(self, node: ast.Call, scope: _Function | None, depth: int) -> set[str]:
        function = node.func
        if isinstance(function, ast.Name) and function.id == "dict":
            found: set[str] = set()
            for argument in node.args:
                found |= self.keys(argument, scope, depth + 1)
            for keyword in node.keywords:
                found |= (
                    {keyword.arg} if keyword.arg else self.keys(keyword.value, scope, depth + 1)
                )
            return found
        if isinstance(function, ast.Attribute) and function.attr == "dumps" and node.args:
            return self.keys(node.args[0], scope, depth + 1)  # json.dumps({...})
        name = ""
        if isinstance(function, ast.Name):
            name = function.id
        elif (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id in {"self", "cls"}
        ):
            name = function.attr
        helpers = self.functions.get(name, [])
        if not helpers:
            return {MORE}
        found = set()
        for helper in helpers:
            for inner in _own_nodes(helper):
                if isinstance(inner, ast.Return):
                    found |= self.keys(inner.value, helper, depth + 1)
        return found or {MORE}

    def _name(self, name: str, scope: _Function | None, depth: int) -> set[str]:
        if scope is None:
            return {MORE}
        marker = (id(scope), name)
        if marker in self._resolving:
            return set()  # payload = {**payload, ...}: the other assignments supply its keys
        self._resolving.add(marker)
        try:
            found: set[str] = set()
            assigned = False
            for node in _own_nodes(scope):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == name:
                            assigned = True
                            found |= self.keys(node.value, scope, depth + 1)
                        elif (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == name
                        ):
                            key = target.slice
                            is_text = isinstance(key, ast.Constant) and isinstance(key.value, str)
                            found.add(key.value if is_text else MORE)  # type: ignore[union-attr]
                elif (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == name
                    and node.value is not None
                ):
                    assigned = True
                    found |= self.keys(node.value, scope, depth + 1)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == name
                ):
                    if node.func.attr == "update":
                        for argument in node.args:
                            found |= self.keys(argument, scope, depth + 1)
                        found |= {keyword.arg or MORE for keyword in node.keywords}
                    elif node.func.attr == "setdefault" and node.args:
                        key = node.args[0]
                        is_text = isinstance(key, ast.Constant) and isinstance(key.value, str)
                        found.add(key.value if is_text else MORE)  # type: ignore[attr-defined]
            if not assigned:
                found.add(MORE)  # a parameter: the caller decides what it carries
            return found
        finally:
            self._resolving.discard(marker)


def _sse_frames() -> dict[str, Any]:
    """The server-sent-event frames of the run and conversation event streams."""
    path = SOURCE / "api" / "app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    reader = _PayloadKeys(tree, [])
    frames: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or "sse" not in node.name:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.JoinedStr):
                frames[node.name] = "".join(
                    str(part.value)
                    if isinstance(part, ast.Constant)
                    else "{" + ast.unparse(part.value) + "}"  # type: ignore[attr-defined]
                    for part in inner.values
                )
            if isinstance(inner, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "payload" for target in inner.targets
            ):
                keys = reader.keys(inner.value, node)
                if keys != {MORE}:
                    frames[f"{node.name} data"] = sorted(keys)
    for inner in ast.walk(tree):
        if (
            isinstance(inner, ast.Constant)
            and isinstance(inner.value, bytes)
            and inner.value.startswith(b":")
        ):
            frames["comment"] = inner.value.decode()
    return dict(sorted(frames.items()))


def event_payloads() -> dict[str, Any]:
    """The payload shapes of the public events.

    ``envelopes``: the fields of each event model, and the keys every workflow event carries.
    ``run``, ``conversation``, ``workflow``: by event type, the keys its ``data`` can carry,
    read from every place the Runtime emits it (``*``: also keys this script cannot name).
    ``sse``: the frames of ``GET /v1/runs/{run_id}/events`` and
    ``GET /v1/conversations/{conversation_id}/events``.
    """
    from algen_agent_runtime.conversations import ConversationEvent
    from algen_agent_runtime.events.contracts import AuditEvent, RunEvent
    from algen_agent_runtime.frameworks import FrameworkStreamEvent

    trees = dict(_trees())
    payloads: dict[str, dict[str, set[str]]] = {family: {} for family in EVENT_FAMILIES.values()}
    workflow_envelope: set[str] = set()
    for path, tree in trees.items():
        package = path.relative_to(SOURCE).parts[0]
        family = EVENT_FAMILIES.get(package)
        if family is None:
            continue
        siblings = [
            other
            for other_path, other in trees.items()
            if other_path.parent == path.parent and other_path != path
        ]
        reader = _PayloadKeys(tree, siblings)
        for node in ast.walk(tree):
            if (
                family == "workflow"
                and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_emit"
            ):
                workflow_envelope |= reader.keys(ast.Name(id="payload"), node) - {MORE}
            if not isinstance(node, ast.Call) or _function_name(node) not in PAYLOAD_EMITTERS:
                continue
            position = next(
                (index for index, argument in enumerate(node.args) if _event_names(argument)),
                None,
            )
            if position is None:
                continue
            names = _event_names(node.args[position])
            definition = reader.emitter(_function_name(node))
            parameters = _parameters(definition) if definition else []
            if position < len(parameters) and parameters[position] == "event_type":
                bound: dict[str, ast.expr] = dict(zip(parameters, node.args, strict=False))
                bound.update(
                    {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                )
                data = bound.get("data")
            elif len(node.args) > position + 1:
                data = node.args[position + 1]
            else:
                data = next((k.value for k in node.keywords if k.arg == "data"), None)
            keys = reader.keys(data, reader.enclosing(node))
            for name in names:
                payloads[family].setdefault(name, set()).update(keys)

    def model(cls: type) -> dict[str, str]:
        return typing.cast(dict[str, str], _describe(cls)["model"])

    return {
        "envelopes": {
            "audit": model(AuditEvent),
            "conversation": model(ConversationEvent),
            "framework": model(FrameworkStreamEvent),
            "run": model(RunEvent),
            "workflow": sorted(workflow_envelope),
        },
        **{
            family: {name: sorted(keys) for name, keys in sorted(found.items())}
            for family, found in payloads.items()
        },
        "sse": _sse_frames(),
    }


def experimental(
    surface: dict[str, Any], configuration_paths: list[str], protocol_names: list[str]
) -> dict[str, list[str]]:
    """Every experimental entry, by section."""
    return {
        "configuration": configuration_paths,
        "exports": sorted(
            f"{module}.{name}"
            for module, names in surface["exports"].items()
            for name, entry in names.items()
            if entry.get("stability") == EXPERIMENTAL
        ),
        "protocols": protocol_names,
    }


def surface() -> dict[str, Any]:
    events, codes = identifiers()
    fields, experimental_fields = _configuration()
    found: dict[str, Any] = {
        "exports": exports(),
        "http_routes": routes(),
        "configuration": fields,
        "errors": errors(),
        "protocols": protocols(),
        "events": events,
        "event_payloads": event_payloads(),
        "check_codes": codes,
        "migrations": sorted(
            path.name for path in (SOURCE / "persistence" / "migrations").glob("*.sql")
        ),
    }
    found["experimental"] = experimental(
        found,
        experimental_fields,
        sorted(name for name, cls in _protocol_classes() if _is_experimental(cls)),
    )
    return found


def _entries(value: dict[str, Any]) -> dict[str, Any]:
    """Each entry of a surface by its path (``section/name``), for ``changes``."""
    flat: dict[str, Any] = {}
    for section, content in value.items():
        if section == EXPERIMENTAL:
            continue
        if section in {"exports", "event_payloads"}:
            separator = "." if section == "exports" else "/"
            for group, entries in content.items():
                for name, entry in entries.items():
                    flat[f"{section}/{group}{separator}{name}"] = entry
        elif isinstance(content, dict):
            for name, entry in content.items():
                flat[f"{section}/{name}"] = entry
        else:
            for item in content:
                flat[f"{section}/{item}"] = True
    return flat


def _experimental_paths(value: dict[str, Any]) -> set[str]:
    return {
        f"{section}/{name}"
        for section, names in value.get(EXPERIMENTAL, {}).items()
        for name in names
    }


def changes(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, list[str]]:
    """How ``current`` differs from ``baseline``, grouped as in ``CHANGE_GROUPS``.

    An entry experimental in ``baseline``, or new and experimental in ``current``, goes to
    ``experimental`` whatever happened to it. A stable entry that becomes experimental leaves the
    compatibility promise, so it is ``changed``. A payload that only gains keys is ``added``.
    """
    before, after = _entries(baseline), _entries(current)
    was_experimental = _experimental_paths(baseline)
    is_experimental = _experimental_paths(current)
    grouped: dict[str, list[str]] = {group: [] for group in CHANGE_GROUPS}
    missing = object()
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, missing), after.get(path, missing)
        if old == new:
            continue
        if path in was_experimental or (old is missing and path in is_experimental):
            grouped["experimental"].append(path)
        elif old is missing:
            grouped["added"].append(path)
        elif new is missing:
            grouped["removed"].append(path)
        elif isinstance(old, list) and isinstance(new, list) and set(old) <= set(new):
            grouped["added"].append(path)
        else:
            grouped["changed"].append(path)
    return grouped


def describe_changes(grouped: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for group, paths in grouped.items():
        if paths:
            lines.append(f"{group} ({CHANGE_GROUPS[group]}):")
            lines.extend(f"  {path}" for path in paths)
    return "\n".join(lines)


def render(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--update", action="store_true", help="rewrite docs/api/surface.json")
    action.add_argument(
        "--changes", action="store_true", help="group how the code differs from the file"
    )
    args = parser.parse_args()
    current = surface()
    text = render(current)
    if args.update:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(text, encoding="utf-8")
    elif args.changes:
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        print(describe_changes(changes(baseline, json.loads(text))) or "no changes")
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
