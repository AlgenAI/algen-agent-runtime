from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from algen_agent_runtime.exceptions.errors import ConfigurationError, PolicyDeniedError
from algen_agent_runtime.workflows import (
    LoadedHookProvider,
    WorkflowHookLoader,
    WorkflowHookRegistry,
    load_hook_provider,
)


@pytest.fixture
def mock_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register dummy modules in sys.modules for loader tests."""
    mod = types.ModuleType("trusted_pkg")
    mod.__file__ = "/workspace/trusted_pkg/__init__.py"
    mod.__package__ = "trusted_pkg"

    submod = types.ModuleType("trusted_pkg.hooks")
    submod.__file__ = "/workspace/trusted_pkg/hooks.py"
    submod.__package__ = "trusted_pkg"

    def valid_factory(**kwargs: Any) -> WorkflowHookRegistry:
        reg = WorkflowHookRegistry()
        return reg

    valid_factory.__api_version__ = "1.0"  # type: ignore[attr-defined]

    async def async_valid_factory(**kwargs: Any) -> WorkflowHookRegistry:
        reg = WorkflowHookRegistry()
        return reg

    async_valid_factory.__api_version__ = "v1"  # type: ignore[attr-defined]

    class HookContainer:
        def __init__(self) -> None:
            self.hooks = WorkflowHookRegistry()

    def container_factory(**kwargs: Any) -> HookContainer:
        return HookContainer()

    def invalid_return_factory(**kwargs: Any) -> str:
        return "not-a-registry"

    def bad_version_factory(**kwargs: Any) -> WorkflowHookRegistry:
        return WorkflowHookRegistry()

    bad_version_factory.__api_version__ = "99.0"  # type: ignore[attr-defined]

    submod.valid_factory = valid_factory  # type: ignore[attr-defined]
    submod.async_valid_factory = async_valid_factory  # type: ignore[attr-defined]
    submod.container_factory = container_factory  # type: ignore[attr-defined]
    submod.invalid_return_factory = invalid_return_factory  # type: ignore[attr-defined]
    submod.bad_version_factory = bad_version_factory  # type: ignore[attr-defined]
    submod.not_a_callable = "just_a_string"  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "trusted_pkg", mod)
    monkeypatch.setitem(sys.modules, "trusted_pkg.hooks", submod)


def test_empty_allowlist_rejects_any_dynamic_import() -> None:
    loader = WorkflowHookLoader(allowed_modules=())
    assert loader.allowed_modules == ()
    with pytest.raises(PolicyDeniedError, match="is not in the allowed modules"):
        loader.load("trusted_pkg.hooks:valid_factory")


def test_prefix_matching_prevents_prefix_confusion() -> None:
    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg.hooks",))
    # Exact match allowed
    assert loader.is_module_allowed("trusted_pkg.hooks") is True
    # Submodule allowed
    assert loader.is_module_allowed("trusted_pkg.hooks.extra") is True
    # Prefix confusion denied
    assert loader.is_module_allowed("trusted_pkg.hooks_bypass") is False
    assert loader.is_module_allowed("trusted_pkg") is False


def test_unauthorized_module_rejected_before_import(monkeypatch: pytest.MonkeyPatch) -> None:
    import_called = False
    real_import = __import__

    def tracked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal import_called
        if "untrusted_pkg" in name:
            import_called = True
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", tracked_import)

    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg",))
    with pytest.raises(PolicyDeniedError) as exc_info:
        loader.load("untrusted_pkg.danger:exploit")

    assert "is not in the allowed modules" in str(exc_info.value)
    assert not import_called


def test_invalid_reference_formats() -> None:
    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg",))

    with pytest.raises(ConfigurationError, match="must be in 'module:callable' format"):
        loader.load("no_colon_here")

    with pytest.raises(ConfigurationError, match="missing module or factory name"):
        loader.load(":callable_only")

    with pytest.raises(ConfigurationError, match="missing module or factory name"):
        loader.load("module_only:")


def test_missing_or_invalid_factory_attributes(mock_modules: None) -> None:
    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg",))

    # Missing attribute
    with pytest.raises(ConfigurationError, match="has no attribute 'missing_func'"):
        loader.load("trusted_pkg.hooks:missing_func")

    # Non-callable attribute
    with pytest.raises(ConfigurationError, match="is not callable"):
        loader.load("trusted_pkg.hooks:not_a_callable")

    # Incompatible API version
    with pytest.raises(ConfigurationError, match=r"unsupported hook provider API version '99\.0'"):
        loader.load("trusted_pkg.hooks:bad_version_factory")

    # Invalid return type
    with pytest.raises(ConfigurationError, match="expected WorkflowHookRegistry"):
        loader.load("trusted_pkg.hooks:invalid_return_factory")


def test_successful_sync_load(mock_modules: None) -> None:
    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg",))
    loaded = loader.load("trusted_pkg.hooks:valid_factory", extra_kwarg="test")

    assert isinstance(loaded, LoadedHookProvider)
    assert isinstance(loaded.hooks, WorkflowHookRegistry)
    assert loaded.reference == "trusted_pkg.hooks:valid_factory"
    assert loaded.provider == "trusted_pkg.hooks:valid_factory"
    assert loaded.module_name == "trusted_pkg.hooks"
    assert loaded.factory_name == "valid_factory"
    assert loaded.api_version == "1.0"
    assert loaded.package_name == "trusted_pkg"

    meta = loaded.audit_metadata
    assert meta["hook_provider"] == "trusted_pkg.hooks:valid_factory"
    assert meta["module"] == "trusted_pkg.hooks"
    assert meta["factory"] == "valid_factory"
    assert meta["api_version"] == "1.0"
    assert meta["package"] == "trusted_pkg"


def test_container_wrapper_load(mock_modules: None) -> None:
    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg",))
    loaded = loader.load("trusted_pkg.hooks:container_factory")

    assert isinstance(loaded.hooks, WorkflowHookRegistry)


@pytest.mark.asyncio
async def test_successful_async_load(mock_modules: None) -> None:
    loader = WorkflowHookLoader(allowed_modules=("trusted_pkg",))
    loaded = await loader.aload("trusted_pkg.hooks:async_valid_factory", extra="async")

    assert isinstance(loaded.hooks, WorkflowHookRegistry)
    assert loaded.api_version == "v1"


def test_load_hook_provider_convenience_function(mock_modules: None) -> None:
    loaded = load_hook_provider(
        "trusted_pkg.hooks:valid_factory",
        allowed_modules=("trusted_pkg",),
    )
    assert isinstance(loaded.hooks, WorkflowHookRegistry)
