"""Plugin signatures: Ed25519 over the plugin package's identity and every file in it.

A signed plugin package carries ``algen-plugin-signature.json`` in its root directory::

    {"format": "algen-plugin-signature/v1", "publisher": "acme", "name": "crm",
     "version": "1.2.0", "package": "acme_plugins",
     "files": {"__init__.py": "<sha256>", "crm/tools.py": "<sha256>", ...},
     "signature": "<base64 Ed25519 signature>"}

The signature covers the canonical JSON of every field except ``signature`` (sorted keys, no
whitespace, ASCII). ``files`` lists every file under the package root except the signature
file, ``__pycache__`` directories and compiled ``.pyc``/``.pyo`` files, so a changed, added or
removed module, data file or extension module breaks it. The package root is the first regular
package on the plugin's module path; it is found with import finders, without importing
anything, so an unsigned or tampered plugin is refused before any of its code runs. The
publisher must be listed in ``plugins.trusted_publishers`` with its Ed25519 public key.

Bytecode is not signed but is checked: a loose ``.pyc``/``.pyo`` outside ``__pycache__`` (which
Python imports when no source file exists) refuses the package, and every ``__pycache__`` file
for the running interpreter that Python would load in place of a signed source file (its
recorded size and modification time, or source hash, match, or it is an unchecked hash-based
file) must hold the same code as that source compiles to. Stale caches, which Python ignores,
are ignored too.

What a signature does not cover: the plugin's dependencies (other installed packages). Install
signed plugins read-only (or from an image built in CI) so nobody can replace files between
verification and import.

An entry point name is resolved once: a name that more than one installed distribution
declares in the plugin group is refused, and the module that is verified is the module that
is imported (see ``resolve_entry_point``).

``PluginManifest.signature`` predates this format and is not used: a module cannot hold a
signature over its own bytes, and the manifest is only readable after import. After import,
the manifest's ``publisher`` (and ``name`` and ``version`` when signed) must match the
signature.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.util
import json
import logging
import marshal
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from algen_agent_runtime.exceptions.errors import ConfigurationError

LOGGER = logging.getLogger(__name__)

SIGNATURE_FILE = "algen-plugin-signature.json"
SIGNATURE_FORMAT = "algen-plugin-signature/v1"
ENTRY_POINT_GROUP = "algen_agent_runtime.plugins"
_SKIPPED_SUFFIXES = (".pyc", ".pyo")


def _decode_public_key(value: str) -> bytes:
    text = value.strip().removeprefix("ed25519:").replace("-", "+").replace("_", "/")
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), validate=True)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise ValueError("publisher keys must be base64 raw 32-byte Ed25519 public keys")
    return raw


class PluginSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trusted_publishers: dict[str, tuple[str, ...]] = Field(
        default_factory=dict,
        description="Publisher name to its Ed25519 public keys (base64 of the raw 32 bytes, as "
        "'plugin keygen' prints them). A list allows key rotation.",
    )
    require_signed: bool = Field(
        default=False,
        description="Refuse plugins and workflow hook packages that are not signed by a trusted "
        "publisher, before importing them, even when their prefix is trusted.",
    )

    @field_validator("trusted_publishers", mode="before")
    @classmethod
    def _keys_as_lists(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {name: [keys] if isinstance(keys, str) else keys for name, keys in value.items()}
        return value

    @field_validator("trusted_publishers")
    @classmethod
    def _valid_keys(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for publisher, keys in value.items():
            if not publisher.strip() or not keys:
                raise ValueError("each trusted publisher needs a name and at least one key")
            for key in keys:
                _decode_public_key(key)
        return value

    @property
    def enabled(self) -> bool:
        return self.require_signed or bool(self.trusted_publishers)


@dataclass(frozen=True)
class SignedPackage:
    """A verified signature."""

    publisher: str
    name: str | None
    version: str | None
    package: str
    root: Path
    files: int


def _ed25519() -> Any:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:
        raise ConfigurationError(
            "plugin signatures need the cryptography package; install "
            "algen-agent-runtime[encryption]"
        ) from exc
    return ed25519, InvalidSignature


def canonical_payload(document: Mapping[str, Any]) -> bytes:
    payload = {key: value for key, value in document.items() if key != "signature"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def package_files(root: Path) -> dict[str, str]:
    """SHA-256 of every file under ``root`` that a signature covers, by POSIX relative path."""
    digests: dict[str, str] = {}
    for directory, subdirectories, filenames in os.walk(root):
        base = Path(directory)
        for name in list(subdirectories):
            if (base / name).is_symlink():
                raise ConfigurationError(
                    f"plugin package {root} contains a symlinked directory {name!r}; signed "
                    "packages must not"
                )
        subdirectories[:] = sorted(name for name in subdirectories if name != "__pycache__")
        for filename in sorted(filenames):
            if filename == SIGNATURE_FILE:
                continue
            if filename.endswith(_SKIPPED_SUFFIXES):
                raise ConfigurationError(
                    f"plugin package {root} contains compiled bytecode "
                    f"{(base / filename).relative_to(root).as_posix()!r} outside __pycache__, "
                    "which Python can import without its source; remove it"
                )
            path = base / filename
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 16), b""):
                    digest.update(chunk)
            digests[path.relative_to(root).as_posix()] = digest.hexdigest()
    return digests


def _find_spec(fullname: str, path: list[str] | None) -> importlib.machinery.ModuleSpec | None:
    """A module's spec from the import finders, without importing it or its parents."""
    if path is None:
        return importlib.util.find_spec(fullname) if "." not in fullname else None
    for finder in sys.meta_path:
        find_spec = getattr(finder, "find_spec", None)
        if find_spec is None:
            continue
        spec = find_spec(fullname, path)
        if spec is not None:
            return spec  # type: ignore[no-any-return]
    return None


def locate_package(module: str) -> tuple[str, Path]:
    """The dotted name and directory of the first regular package on ``module``'s path."""
    parts = module.strip().split(".")
    path: list[str] | None = None
    for index in range(len(parts)):
        name = ".".join(parts[: index + 1])
        try:
            spec = _find_spec(name, path)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            raise ConfigurationError(f"plugin module {module!r} was not found")
        locations = spec.submodule_search_locations
        if locations is not None and spec.origin in (None, "namespace"):
            path = list(locations)
            continue
        if locations is not None and spec.origin is not None:
            return name, Path(spec.origin).parent
        raise ConfigurationError(
            f"plugin module {module!r} is a single-file module; signed plugins must be packages"
        )
    raise ConfigurationError(f"plugin module {module!r} has no regular package to verify")


def _distribution_name(entry: metadata.EntryPoint) -> str:
    dist = getattr(entry, "dist", None)
    name = getattr(dist, "name", None) if dist is not None else None
    return str(name) if name else "an unknown distribution"


def resolve_entry_point(name: str) -> metadata.EntryPoint:
    """The one installed entry point called ``name`` in the plugin group.

    Refuses a name that several distributions declare, so the entry point whose module is
    trusted and verified is the one that is loaded.
    """
    matches = [
        entry for entry in metadata.entry_points(group=ENTRY_POINT_GROUP) if entry.name == name
    ]
    if not matches:
        raise ConfigurationError(
            f"plugin entry point {name!r} was not found in group {ENTRY_POINT_GROUP!r}"
        )
    if len(matches) > 1:
        declared = ", ".join(f"{_distribution_name(entry)} ({entry.value})" for entry in matches)
        raise ConfigurationError(
            f"plugin entry point {name!r} is declared by more than one installed distribution: "
            f"{declared}; uninstall one, or name the plugin as 'module:attribute'"
        )
    return matches[0]


def plugin_module(reference: str) -> str:
    """The module a plugin reference imports: ``module:attribute`` or an entry point name."""
    if ":" in reference:
        return reference.split(":", 1)[0].strip()
    return resolve_entry_point(reference.strip()).module


def _bytecode_source(cache: Path) -> tuple[str, int] | None:
    """The source file name and optimization level of a ``__pycache__`` file for this Python."""
    tag = sys.implementation.cache_tag
    if tag is None:
        return None
    match = re.fullmatch(
        rf"(?P<stem>.+)\.{re.escape(tag)}(?:\.opt-(?P<opt>[12]))?\.pyc", cache.name
    )
    if match is None:
        return None
    return match.group("stem") + ".py", int(match.group("opt") or 0)


def _loaded_instead_of(source: Path, data: bytes) -> bool:
    """Whether Python would load this cache file in place of ``source``."""
    if len(data) < 16 or data[:4] != importlib.util.MAGIC_NUMBER:
        return False
    flags = int.from_bytes(data[4:8], "little")
    if flags & ~0b11:
        return False
    if flags & 0b1:
        if not flags & 0b10:
            return True  # unchecked hash-based: loaded without looking at the source
        return data[8:16] == importlib.util.source_hash(source.read_bytes())
    stat = source.stat()
    return (
        int.from_bytes(data[8:12], "little") == int(stat.st_mtime) & 0xFFFFFFFF
        and int.from_bytes(data[12:16], "little") == stat.st_size & 0xFFFFFFFF
    )


def check_bytecode(root: Path, signed_files: Mapping[str, Any]) -> None:
    """Refuse ``__pycache__`` files that Python would run instead of a signed source file
    unless they hold the code that source compiles to."""
    for cache in sorted(root.rglob("__pycache__/*")):
        if not cache.is_file() or cache.parent.name != "__pycache__":
            continue
        located = _bytecode_source(cache)
        if located is None:
            continue
        filename, optimize = located
        source = cache.parent.parent / filename
        relative = source.relative_to(root).as_posix()
        if relative not in signed_files or not source.is_file():
            continue
        data = cache.read_bytes()
        if not _loaded_instead_of(source, data):
            continue
        try:
            matches = marshal.loads(data[16:]) == compile(
                source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=optimize
            )
        except Exception:  # an unreadable cache is refused like a mismatched one
            matches = False
        if not matches:
            raise ConfigurationError(
                f"plugin package {root.name!r}: bytecode cache "
                f"{cache.relative_to(root).as_posix()!r} does not match its signed source "
                f"{relative!r}; delete the __pycache__ directories and install the plugin "
                "read-only"
            )


def verify_module(module: str, settings: PluginSettings) -> SignedPackage | None:
    """Verify the package that ``module`` belongs to, before it is imported.

    Returns the signature when it verifies; None for an unsigned package (or one from an
    unknown publisher) when signatures are not required. Raises ``ConfigurationError`` for a
    signature that does not verify, and for any unsigned package under ``require_signed``.
    """
    package, root = locate_package(module)
    signature_path = root / SIGNATURE_FILE
    if not signature_path.is_file():
        if settings.require_signed:
            raise ConfigurationError(
                f"plugin package {package!r} is not signed and plugins.require_signed is set"
            )
        return None
    try:
        document = json.loads(signature_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"plugin package {package!r} has an unreadable signature") from exc
    if not isinstance(document, dict) or document.get("format") != SIGNATURE_FORMAT:
        raise ConfigurationError(f"plugin package {package!r} has an unknown signature format")
    publisher = document.get("publisher")
    keys = settings.trusted_publishers.get(publisher) if isinstance(publisher, str) else None
    if keys is None:
        if settings.require_signed:
            raise ConfigurationError(
                f"plugin package {package!r} is signed by {publisher!r}, which is not in "
                "plugins.trusted_publishers"
            )
        LOGGER.warning(
            "plugin package %s is signed by an untrusted publisher %r; loading it unverified",
            package,
            publisher,
        )
        return None
    if document.get("package") != package:
        raise ConfigurationError(
            f"plugin package {package!r} carries a signature for {document.get('package')!r}"
        )
    ed25519, invalid_signature = _ed25519()
    try:
        signature = base64.b64decode(str(document.get("signature", "")), validate=True)
    except ValueError:
        signature = b""
    payload = canonical_payload(document)
    for key in keys:
        public_key = ed25519.Ed25519PublicKey.from_public_bytes(_decode_public_key(key))
        try:
            public_key.verify(signature, payload)
            break
        except invalid_signature:
            continue
    else:
        raise ConfigurationError(
            f"plugin package {package!r}: the signature does not verify with {publisher!r}'s keys"
        )
    signed_files = document.get("files")
    actual = package_files(root)
    if not isinstance(signed_files, dict) or signed_files != actual:
        expected = signed_files if isinstance(signed_files, dict) else {}
        changed = sorted(
            name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name)
        )
        raise ConfigurationError(
            f"plugin package {package!r} does not match its signature; changed, added or "
            f"removed: {', '.join(changed[:5])}{' ...' if len(changed) > 5 else ''}"
        )
    check_bytecode(root, actual)
    name = document.get("name")
    version = document.get("version")
    return SignedPackage(
        publisher=str(publisher),
        name=name if isinstance(name, str) else None,
        version=version if isinstance(version, str) else None,
        package=package,
        root=root,
        files=len(actual),
    )


def load_private_key(text: str) -> Any:
    """An Ed25519 private key from PKCS#8 PEM or base64 of the raw 32 bytes."""
    ed25519, _ = _ed25519()
    stripped = text.strip()
    if stripped.startswith("-----BEGIN"):
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(stripped.encode(), password=None)
        if not isinstance(key, ed25519.Ed25519PrivateKey):
            raise ConfigurationError("the signing key is not an Ed25519 key")
        return key
    try:
        raw = base64.b64decode(stripped, validate=True)
    except ValueError:
        raw = b""
    if len(raw) != 32:
        raise ConfigurationError("the signing key must be PKCS#8 PEM or base64 of 32 bytes")
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


def public_key_text(private_key: Any) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def generate_private_key_pem() -> tuple[bytes, str]:
    """A new Ed25519 key as unencrypted PKCS#8 PEM, and its public key text."""
    ed25519, _ = _ed25519()
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    return pem, public_key_text(key)


def sign_package(
    root: Path,
    private_key: Any,
    *,
    publisher: str,
    package: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Write ``algen-plugin-signature.json`` into ``root`` and return it."""
    if not (root / "__init__.py").is_file():
        raise ConfigurationError(f"{root} is not a regular package (no __init__.py)")
    document: dict[str, Any] = {
        "format": SIGNATURE_FORMAT,
        "publisher": publisher,
        "package": package or root.name,
        "files": package_files(root),
    }
    if name is not None:
        document["name"] = name
    if version is not None:
        document["version"] = version
    document["signature"] = base64.b64encode(private_key.sign(canonical_payload(document))).decode()
    (root / SIGNATURE_FILE).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return document
