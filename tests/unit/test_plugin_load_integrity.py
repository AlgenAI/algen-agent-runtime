"""The plugin that is trusted and verified is the plugin that is imported."""

from __future__ import annotations

import importlib
import importlib.util
import py_compile
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.plugins.sdk import PluginLoader
from algen_agent_runtime.plugins.signing import (
    PluginSettings,
    generate_private_key_pem,
    load_private_key,
    sign_package,
    verify_module,
)

PLUGIN = """
from algen_agent_runtime.plugins.sdk import PluginManifest


class P:
    manifest = PluginManifest(name={name!r}, version="1.0.0", publisher="acme")

    def register(self, context):
        context.registered.append({name!r})
"""


class _Context:
    def __init__(self) -> None:
        self.registered: list[str] = []


def _install(site: Path, dist: str, package: str, entry_name: str) -> None:
    (site / package).mkdir()
    (site / package / "__init__.py").write_text(PLUGIN.format(name=package))
    info = site / f"{dist}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text(
        f"[algen_agent_runtime.plugins]\n{entry_name} = {package}:P\n"
    )


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    yield tmp_path
    for module in [m for m in sys.modules if m.startswith(("goodpkg_", "evilpkg_"))]:
        del sys.modules[module]


def _names() -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"goodpkg_{suffix}", f"evilpkg_{suffix}", f"crm_{suffix}"


def test_an_entry_point_declared_by_two_distributions_is_refused(site: Path) -> None:
    good, evil, entry = _names()
    _install(site, good, good, entry)
    _install(site, evil, evil, entry)
    loader = PluginLoader((good, evil))
    with pytest.raises(ConfigurationError, match="more than one installed distribution") as err:
        loader.load((entry,), _Context())  # type: ignore[arg-type]
    assert good in str(err.value) and evil in str(err.value)
    assert good not in sys.modules and evil not in sys.modules


def test_entry_point_trust_is_checked_against_the_module_it_imports(site: Path) -> None:
    good, evil, _ = _names()
    # The entry point's name looks trusted, but it imports an untrusted module.
    _install(site, evil, evil, f"{good}.crm")
    loader = PluginLoader((good,))
    with pytest.raises(ConfigurationError, match="not from a trusted namespace"):
        loader.load((f"{good}.crm",), _Context())  # type: ignore[arg-type]
    assert evil not in sys.modules


def test_a_trusted_entry_point_loads_its_own_module(site: Path) -> None:
    good, _, entry = _names()
    _install(site, good, good, entry)
    context = _Context()
    manifests = PluginLoader((good,)).load((entry,), context)  # type: ignore[arg-type]
    assert manifests[0].name == good and context.registered == [good]


@pytest.fixture
def signed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, Path, str]]:
    pytest.importorskip("cryptography")
    name = f"signedbc_{uuid.uuid4().hex[:8]}"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("")
    (root / "plugin.py").write_text("VALUE = 'signed'\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    pem, public_key = generate_private_key_pem()
    sign_package(root, load_private_key(pem.decode()), publisher="acme")
    yield name, root, public_key
    for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[module]


def _settings(public_key: str) -> PluginSettings:
    return PluginSettings(trusted_publishers={"acme": public_key}, require_signed=True)


def _compile_other_code(root: Path, source: Path, code: str, **kwargs: object) -> Path:
    """Compile ``code`` into the cache file Python reads for ``source``."""
    cache = Path(importlib.util.cache_from_source(str(source)))
    other = root.parent / "other_src.py"
    other.write_text(code)
    py_compile.compile(str(other), cfile=str(cache), **kwargs)  # type: ignore[arg-type]
    other.unlink()
    return cache


def test_a_planted_bytecode_cache_is_refused(signed: tuple[str, Path, str]) -> None:
    name, root, public_key = signed
    source = root / "plugin.py"
    cache = _compile_other_code(root, source, "VALUE = 'evil!!'\n")
    # Give the cache the signed source's size and mtime, so Python would load it.
    data = bytearray(cache.read_bytes())
    stat = source.stat()
    data[8:12] = (int(stat.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
    data[12:16] = (stat.st_size & 0xFFFFFFFF).to_bytes(4, "little")
    cache.write_bytes(bytes(data))
    with pytest.raises(ConfigurationError, match="does not match its signed source"):
        verify_module(f"{name}.plugin", _settings(public_key))


def test_an_unchecked_hash_cache_with_other_code_is_refused(
    signed: tuple[str, Path, str],
) -> None:
    name, root, public_key = signed
    _compile_other_code(
        root,
        root / "plugin.py",
        "VALUE = 'evil'\n",
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    with pytest.raises(ConfigurationError, match="does not match its signed source"):
        verify_module(f"{name}.plugin", _settings(public_key))


def test_a_stale_cache_python_would_ignore_is_ignored(signed: tuple[str, Path, str]) -> None:
    name, root, public_key = signed
    _compile_other_code(root, root / "plugin.py", "VALUE = 'old version'\n")
    assert verify_module(f"{name}.plugin", _settings(public_key)) is not None


@pytest.mark.parametrize(
    "mode",
    [
        py_compile.PycInvalidationMode.TIMESTAMP,
        py_compile.PycInvalidationMode.CHECKED_HASH,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
def test_a_genuine_bytecode_cache_is_accepted(
    signed: tuple[str, Path, str], mode: py_compile.PycInvalidationMode
) -> None:
    name, root, public_key = signed
    py_compile.compile(str(root / "plugin.py"), invalidation_mode=mode)
    assert verify_module(f"{name}.plugin", _settings(public_key)) is not None


def test_loose_bytecode_outside_pycache_is_refused(signed: tuple[str, Path, str]) -> None:
    name, root, public_key = signed
    (root / "backdoor.pyc").write_bytes(b"\x00" * 16)
    with pytest.raises(ConfigurationError, match="outside __pycache__"):
        verify_module(f"{name}.plugin", _settings(public_key))
