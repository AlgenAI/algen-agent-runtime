from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from importlib import metadata
from typing import Annotated, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.plugins.signing import (
    PluginSettings,
    SignedPackage,
    resolve_entry_point,
    verify_module,
)

ENTRY_POINT_GROUP = "algen_agent_runtime.plugins"
CORE_API_VERSION = "1"


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    version: str
    core_api_version: str = CORE_API_VERSION
    publisher: str
    signature: str | None = Field(
        default=None,
        description="Unused: signatures live in the package's algen-plugin-signature.json, "
        "verified before import (see algen_agent_runtime.plugins.signing).",
    )
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    env: frozenset[Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]] = Field(
        default_factory=frozenset,
        description="Environment variable names the plugin's components read, so a deploy "
        "can ask for them.",
    )


class PluginRegistry(Protocol):
    def register(self, *args: Any, **kwargs: Any) -> None: ...


class PluginContext(Protocol):
    """The registries a plugin's ``register(context)`` adds components to.

    Every registry refuses a name that is already registered, whether it came from the
    Runtime, the manifest or an earlier plugin.
    """

    @property
    def tools(self) -> PluginRegistry: ...

    @property
    def planners(self) -> PluginRegistry: ...

    @property
    def contexts(self) -> PluginRegistry: ...

    @property
    def verifiers(self) -> PluginRegistry: ...

    @property
    def composers(self) -> PluginRegistry: ...

    @property
    def semantics(self) -> PluginRegistry: ...

    @property
    def methods(self) -> PluginRegistry: ...

    @property
    def model_services(self) -> PluginRegistry: ...

    @property
    def providers(self) -> PluginRegistry: ...

    @property
    def mcp_servers(self) -> PluginRegistry: ...

    @property
    def policies(self) -> PluginRegistry: ...

    @property
    def analytical_nodes(self) -> PluginRegistry: ...

    @property
    def retrievers(self) -> PluginRegistry: ...

    @property
    def secrets(self) -> Callable[[str], Awaitable[str]]:
        """Resolves ``env://`` references with the container's environment; call it only when
        a component runs or starts, never in ``register``."""
        ...

    def declare_env(self, *names: str) -> None: ...

    def add_resource(self, resource: object) -> None: ...


class AlgenAgentRuntimePlugin(Protocol):
    manifest: PluginManifest

    def register(self, context: PluginContext) -> None: ...


def is_trusted(reference: str, trusted_prefixes: tuple[str, ...]) -> bool:
    """Match a plugin reference against trusted prefixes on module boundaries.

    ``acme.plugins`` (or ``acme.plugins.``) trusts ``acme.plugins``, ``acme.plugins.crm`` and
    ``acme.plugins.crm:CrmPlugin``, but not ``acme.plugins_evil``.
    """
    name = reference.split(":", 1)[0].strip()
    for prefix in trusted_prefixes:
        cleaned = prefix.strip().rstrip(".")
        if cleaned and (name == cleaned or name.startswith(cleaned + ".")):
            return True
    return False


class PluginLoader:
    """Load plugins named by ``module:attribute`` or by an installed entry point name.

    Trust is checked before anything is imported: the module prefix must be trusted (for an
    entry point name, the prefix of the module the entry point imports) and, with
    ``signing`` (``plugins`` settings), the plugin package's signature must verify against a
    trusted publisher's key (see ``algen_agent_runtime.plugins.signing``). Neither is
    sandboxing: a trusted plugin runs with the Runtime's privileges, so untrusted code must
    run in an isolated process.
    """

    def __init__(
        self, trusted_prefixes: tuple[str, ...], signing: PluginSettings | None = None
    ) -> None:
        self._trusted_prefixes = trusted_prefixes
        self._signing = signing
        self._verified: dict[str, SignedPackage | None] = {}
        self._entries: dict[str, metadata.EntryPoint] = {}

    def load(self, names: tuple[str, ...], context: PluginContext) -> tuple[PluginManifest, ...]:
        self.authorize(names)
        return tuple(self.load_one(name, context) for name in names)

    def authorize(self, names: tuple[str, ...]) -> None:
        """Refuse the whole set before importing any of it."""
        if not self._trusted_prefixes and names:
            raise ConfigurationError(
                "domain_plugins requires security.trusted_plugin_prefixes to list the "
                "packages plugins may be loaded from"
            )
        for name in names:
            self._check_trusted(name)
        for name in names:
            self._verify(name)

    def _entry_point(self, name: str) -> metadata.EntryPoint:
        """Resolve an entry point name once, so the module checked is the module loaded."""
        key = name.strip()
        if key not in self._entries:
            self._entries[key] = resolve_entry_point(key)
        return self._entries[key]

    def _module(self, name: str) -> str:
        if ":" in name:
            return name.split(":", 1)[0].strip()
        return self._entry_point(name).module

    def _check_trusted(self, name: str) -> None:
        module = self._module(name)
        if not is_trusted(module, self._trusted_prefixes):
            target = "" if ":" in name else f" (entry point for module {module!r})"
            raise ConfigurationError(
                f"plugin {name!r}{target} is not from a trusted namespace; add its package to "
                "security.trusted_plugin_prefixes"
            )

    def _verify(self, name: str) -> SignedPackage | None:
        if self._signing is None or not self._signing.enabled:
            return None
        if name not in self._verified:
            self._verified[name] = verify_module(self._module(name), self._signing)
        return self._verified[name]

    def load_one(self, name: str, context: PluginContext) -> PluginManifest:
        self._check_trusted(name)
        signed = self._verify(name)
        loaded = _resolve(name) if ":" in name else _load_entry(name, self._entry_point(name))
        instance = _instantiate(name, loaded)
        manifest = getattr(instance, "manifest", None)
        if not isinstance(manifest, PluginManifest) or not callable(
            getattr(instance, "register", None)
        ):
            raise ConfigurationError(
                f"plugin {name!r} must have a PluginManifest and a register(context) method"
            )
        if manifest.core_api_version != CORE_API_VERSION:
            raise ConfigurationError(f"plugin {name!r} targets an incompatible core API")
        if signed is not None and (
            manifest.publisher != signed.publisher
            or (signed.name is not None and manifest.name != signed.name)
            or (signed.version is not None and manifest.version != signed.version)
        ):
            raise ConfigurationError(
                f"plugin {name!r}: its manifest does not match the publisher, name and version "
                "it was signed with"
            )
        instance.register(context)
        return manifest


def _resolve(name: str) -> Any:
    if ":" in name:
        module_name, attribute = (part.strip() for part in name.split(":", 1))
        if not module_name or not attribute:
            raise ConfigurationError(f"plugin reference {name!r} must be 'module:attribute'")
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            raise ConfigurationError(
                f"could not import plugin module {module_name!r}: {exc}; install its package "
                "or add the directory that contains it to the import path (--path)"
            ) from exc
        except Exception as exc:
            raise ConfigurationError(
                f"could not import plugin module {module_name!r}: {exc}"
            ) from exc
        if not hasattr(module, attribute):
            raise ConfigurationError(
                f"plugin module {module_name!r} has no attribute {attribute!r}"
            )
        return getattr(module, attribute)
    return _load_entry(name, resolve_entry_point(name.strip()))


def _load_entry(name: str, entry: metadata.EntryPoint) -> Any:
    try:
        return entry.load()
    except Exception as exc:
        raise ConfigurationError(f"could not load plugin entry point {name!r}: {exc}") from exc


def _instantiate(name: str, loaded: Any) -> Any:
    if isinstance(loaded, type) or not hasattr(loaded, "register"):
        if not callable(loaded):
            raise ConfigurationError(f"plugin {name!r} is neither a plugin nor a factory")
        return loaded()
    return loaded


def plugin(manifest: PluginManifest) -> Callable[[type], type]:
    def decorate(cls: type) -> type:
        setattr(cls, "manifest", manifest)  # noqa: B010 - decorator attaches declared metadata
        return cls

    return decorate
