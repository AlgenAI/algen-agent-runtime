from __future__ import annotations

import importlib
import importlib.metadata
import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError
from algen_agent_runtime.workflows.engine import WorkflowHookRegistry


@dataclass(slots=True)
class LoadedHookProvider:
    """Record of a verified, authorized hook provider loaded for a workflow."""

    provider: str
    module_name: str
    factory_name: str
    hooks: WorkflowHookRegistry
    package_name: str | None = None
    package_version: str | None = None
    api_version: str | None = None
    audit_metadata: dict[str, str] = field(default_factory=dict)
    close: Callable[[], Awaitable[None] | None] | None = None

    @property
    def reference(self) -> str:
        """Alias for the provider reference string."""
        return self.provider

    async def aclose(self) -> None:
        """Close application-owned hook resources when the factory supplied one."""
        if self.close is None:
            return
        result = self.close()
        if inspect.isawaitable(result):
            await result


class WorkflowHookLoader:
    """Typed and allowlisted loader for workflow hook providers.

    Enforces explicit module allowlisting before dynamic imports, callable shape checks,
    API version validation, entry-point resolution, and audit metadata generation.

    Allowlisting is an application-level trust and structural check; it is NOT sandboxing
    or cryptographic signature verification. Untrusted code must run in an isolated process
    or container.
    """

    SUPPORTED_API_VERSIONS: frozenset[str] = frozenset({"1", "1.0", "v1"})

    def __init__(
        self,
        *,
        allowed_modules: Sequence[str] = (),
        allowed_entry_point_groups: Sequence[str] = ("algen.workflow_hooks",),
        allow_all_installed_entry_points: bool = False,
    ) -> None:
        self.allowed_modules = tuple(m.strip() for m in allowed_modules if m.strip())
        self.allowed_entry_point_groups = tuple(allowed_entry_point_groups)
        self.allow_all_installed_entry_points = allow_all_installed_entry_points

    def is_module_allowed(self, module_name: str) -> bool:
        """Check if a module name is permitted by the allowlist.

        Matches exact module names or dot-separated subpackages, preventing prefix confusion
        (e.g., 'examples.test' allows 'examples.test.hooks' but rejects 'examples.test_bypass').
        """
        cleaned = module_name.strip()
        if not self.allowed_modules:
            return False
        for allowed in self.allowed_modules:
            if cleaned == allowed or cleaned.startswith(allowed + "."):
                return True
        return False

    def _parse_and_authorize(self, reference: str) -> tuple[str, str]:
        if not reference or ":" not in reference:
            raise ConfigurationError(
                f"invalid hook provider reference {reference!r}; must be in 'module:callable' format"
            )
        module_name, factory_name = reference.strip().split(":", 1)
        module_name = module_name.strip()
        factory_name = factory_name.strip()
        if not module_name or not factory_name:
            raise ConfigurationError(
                f"invalid hook provider reference {reference!r}; missing module or factory name"
            )

        # Check authorization BEFORE any import occurs
        if not self.is_module_allowed(module_name):
            # Check entry-point groups if enabled
            if self.allow_all_installed_entry_points:
                for group in self.allowed_entry_point_groups:
                    try:
                        eps = importlib.metadata.entry_points(group=group)
                        if any(ep.value == reference for ep in eps):
                            return module_name, factory_name
                    except Exception:
                        pass
            raise PolicyDeniedError(
                f"hook provider module {module_name!r} is not in the allowed modules: "
                f"{self.allowed_modules}. Explicit host authorization is required."
            )
        return module_name, factory_name

    def _resolve_factory(self, module_name: str, factory_name: str) -> Any:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise ConfigurationError(
                f"could not import hook provider module {module_name!r}: {exc}"
            ) from exc

        if not hasattr(module, factory_name):
            raise ConfigurationError(f"module {module_name!r} has no attribute {factory_name!r}")

        factory = getattr(module, factory_name)
        if not callable(factory):
            raise ConfigurationError(
                f"hook provider {module_name}:{factory_name} attribute {factory_name!r} is not callable"
            )

        api_version = getattr(factory, "__api_version__", getattr(module, "__api_version__", None))
        if api_version is not None and str(api_version) not in self.SUPPORTED_API_VERSIONS:
            raise ConfigurationError(
                f"unsupported hook provider API version {api_version!r}; "
                f"supported versions: {sorted(self.SUPPORTED_API_VERSIONS)}"
            )

        return factory, module, str(api_version) if api_version is not None else None

    def _extract_hooks(
        self, result: Any, reference: str
    ) -> tuple[WorkflowHookRegistry, Callable[[], Awaitable[None] | None] | None]:
        hooks = getattr(result, "hooks", result)
        if not isinstance(hooks, WorkflowHookRegistry):
            raise ConfigurationError(
                f"hook provider {reference!r} returned {type(result).__name__}, "
                "expected WorkflowHookRegistry or object with .hooks attribute"
            )
        close = getattr(result, "aclose", None)
        if close is not None and not callable(close):
            raise ConfigurationError(
                f"hook provider {reference!r} has a non-callable aclose attribute"
            )
        return hooks, close

    def _discover_package_info(self, module: Any) -> tuple[str | None, str | None]:
        pkg_name = getattr(module, "__package__", None) or module.__name__.split(".")[0]
        pkg_version: str | None = getattr(module, "__version__", None)
        if pkg_version is None and pkg_name:
            try:
                pkg_version = importlib.metadata.version(pkg_name)
            except Exception:
                pkg_version = None
        return pkg_name, pkg_version

    def load(self, reference: str, **factory_kwargs: Any) -> LoadedHookProvider:
        """Synchronously load and validate a hook provider reference."""
        module_name, factory_name = self._parse_and_authorize(reference)
        factory, module, api_version = self._resolve_factory(module_name, factory_name)

        try:
            sig = inspect.signature(factory)
            if factory_kwargs and any(
                p.kind in (p.KEYWORD_ONLY, p.VAR_KEYWORD) or p.name in factory_kwargs
                for p in sig.parameters.values()
            ):
                result = factory(**factory_kwargs)
            else:
                result = factory()
        except TypeError:
            # Fallback invocation
            result = factory()

        if inspect.isawaitable(result):
            raise ConfigurationError(
                f"hook provider {reference!r} returned an awaitable; use aload() instead"
            )

        hooks, close = self._extract_hooks(result, reference)
        pkg_name, pkg_version = self._discover_package_info(module)
        audit_metadata = {
            "hook_provider": reference,
            "module": module_name,
            "factory": factory_name,
            "package": pkg_name or "local",
            "package_version": pkg_version or "unknown",
            "api_version": api_version or "unspecified",
        }

        return LoadedHookProvider(
            provider=reference,
            module_name=module_name,
            factory_name=factory_name,
            hooks=hooks,
            package_name=pkg_name,
            package_version=pkg_version,
            api_version=api_version,
            audit_metadata=audit_metadata,
            close=close,
        )

    async def aload(self, reference: str, **factory_kwargs: Any) -> LoadedHookProvider:
        """Asynchronously load and validate a hook provider reference."""
        module_name, factory_name = self._parse_and_authorize(reference)
        factory, module, api_version = self._resolve_factory(module_name, factory_name)

        try:
            sig = inspect.signature(factory)
            if factory_kwargs and any(
                p.kind in (p.KEYWORD_ONLY, p.VAR_KEYWORD) or p.name in factory_kwargs
                for p in sig.parameters.values()
            ):
                result = factory(**factory_kwargs)
            else:
                result = factory()
        except TypeError:
            result = factory()

        if inspect.isawaitable(result):
            result = await result

        hooks, close = self._extract_hooks(result, reference)
        pkg_name, pkg_version = self._discover_package_info(module)
        audit_metadata = {
            "hook_provider": reference,
            "module": module_name,
            "factory": factory_name,
            "package": pkg_name or "local",
            "package_version": pkg_version or "unknown",
            "api_version": api_version or "unspecified",
        }

        return LoadedHookProvider(
            provider=reference,
            module_name=module_name,
            factory_name=factory_name,
            hooks=hooks,
            package_name=pkg_name,
            package_version=pkg_version,
            api_version=api_version,
            audit_metadata=audit_metadata,
            close=close,
        )


def load_hook_provider(
    reference: str,
    *,
    loader: WorkflowHookLoader | None = None,
    allowed_modules: Sequence[str] = (),
    **factory_kwargs: Any,
) -> LoadedHookProvider:
    """Convenience helper to load a hook provider with explicit allowlist checking."""
    active_loader = loader or WorkflowHookLoader(allowed_modules=allowed_modules)
    return active_loader.load(reference, **factory_kwargs)
