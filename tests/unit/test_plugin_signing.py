"""Plugin signatures are verified before any plugin code is imported."""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from algen_agent_runtime.cli import main
from algen_agent_runtime.config.settings import AppSettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.plugins.sdk import PluginLoader
from algen_agent_runtime.plugins.signing import (
    SIGNATURE_FILE,
    PluginSettings,
    generate_private_key_pem,
    load_private_key,
    sign_package,
    verify_module,
)
from algen_agent_runtime.workflows.service import WorkflowService

PLUGIN_SOURCE = """
from pathlib import Path

from algen_agent_runtime.plugins.sdk import PluginManifest

Path({marker!r}).write_text("imported")


class Plugin:
    manifest = PluginManifest(name="crm", version="1.0.0", publisher={publisher!r})

    def register(self, context):
        context.registered.append(self.manifest.name)
"""


class _Context:
    def __init__(self) -> None:
        self.registered: list[str] = []


@dataclass
class Package:
    name: str
    root: Path
    marker: Path
    key: Any
    public_key: str

    @property
    def reference(self) -> str:
        return f"{self.name}.plugin:Plugin"

    def settings(self, *, require_signed: bool = True, **publishers: str) -> PluginSettings:
        return PluginSettings(
            trusted_publishers=publishers or {"acme": self.public_key},
            require_signed=require_signed,
        )

    def loader(self, settings: PluginSettings) -> PluginLoader:
        return PluginLoader((self.name,), settings)

    def sign(self, **kwargs: Any) -> None:
        sign_package(self.root, self.key, publisher="acme", name="crm", version="1.0.0", **kwargs)


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Package]:
    pytest.importorskip("cryptography")
    name = f"signedplug_{uuid.uuid4().hex[:8]}"
    root = tmp_path / name
    root.mkdir()
    marker = tmp_path / "imported.txt"
    (root / "__init__.py").write_text("")
    (root / "plugin.py").write_text(PLUGIN_SOURCE.format(marker=str(marker), publisher="acme"))
    (root / "data.json").write_text("{}")
    monkeypatch.syspath_prepend(str(tmp_path))
    pem, public_key = generate_private_key_pem()
    yield Package(name, root, marker, load_private_key(pem.decode()), public_key)
    for module in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        del sys.modules[module]


def test_a_signed_plugin_loads(package: Package) -> None:
    package.sign()
    context = _Context()
    manifests = package.loader(package.settings()).load((package.reference,), context)  # type: ignore[arg-type]
    assert manifests[0].name == "crm"
    assert context.registered == ["crm"]


@pytest.mark.parametrize(
    "tamper",
    [
        lambda root: (root / "plugin.py").write_text(
            (root / "plugin.py").read_text() + "\nEVIL = True\n"
        ),
        lambda root: (root / "extra.py").write_text("EVIL = True\n"),
        lambda root: (root / "data.json").unlink(),
        lambda root: (root / "native.so").write_bytes(b"\x7fELF"),
    ],
    ids=["changed", "added", "removed", "extension-module"],
)
def test_a_tampered_plugin_is_refused_before_import(package: Package, tamper: Any) -> None:
    package.sign()
    tamper(package.root)
    with pytest.raises(ConfigurationError, match="does not match its signature"):
        package.loader(package.settings()).load((package.reference,), _Context())  # type: ignore[arg-type]
    assert not package.marker.exists()


def test_bytecode_caches_are_not_signed(package: Package) -> None:
    package.sign()
    (package.root / "__pycache__").mkdir()
    (package.root / "__pycache__" / "plugin.cpython-312.pyc").write_bytes(b"cache")
    assert verify_module(package.reference.split(":")[0], package.settings()) is not None


def test_unsigned_plugins_are_refused_when_signatures_are_required(package: Package) -> None:
    with pytest.raises(ConfigurationError, match="not signed"):
        package.loader(package.settings()).load((package.reference,), _Context())  # type: ignore[arg-type]
    assert not package.marker.exists()


def test_unsigned_plugins_still_load_when_not_required(package: Package) -> None:
    context = _Context()
    package.loader(package.settings(require_signed=False)).load(
        (package.reference,),
        context,  # type: ignore[arg-type]
    )
    assert context.registered == ["crm"]


def test_an_untrusted_or_wrong_key_is_refused(package: Package) -> None:
    package.sign()
    _, other_key = generate_private_key_pem()
    with pytest.raises(ConfigurationError, match="does not verify"):
        package.loader(package.settings(acme=other_key)).load(
            (package.reference,),
            _Context(),  # type: ignore[arg-type]
        )
    with pytest.raises(ConfigurationError, match=r"not in plugins\.trusted_publishers"):
        package.loader(package.settings(globex=package.public_key)).load(
            (package.reference,),
            _Context(),  # type: ignore[arg-type]
        )
    assert not package.marker.exists()


def test_a_forged_signature_field_is_refused(package: Package) -> None:
    package.sign()
    document = json.loads((package.root / SIGNATURE_FILE).read_text())
    document["publisher"] = "acme"
    document["version"] = "9.9.9"  # changes the signed payload
    (package.root / SIGNATURE_FILE).write_text(json.dumps(document))
    with pytest.raises(ConfigurationError, match="does not verify"):
        package.loader(package.settings()).load((package.reference,), _Context())  # type: ignore[arg-type]


def test_the_manifest_must_match_the_signature(package: Package) -> None:
    sign_package(package.root, package.key, publisher="acme", name="crm", version="2.0.0")
    with pytest.raises(ConfigurationError, match="does not match the publisher"):
        package.loader(package.settings()).load((package.reference,), _Context())  # type: ignore[arg-type]


def test_settings_validate_publisher_keys() -> None:
    pytest.importorskip("cryptography")
    with pytest.raises(ValueError, match="32-byte"):
        AppSettings.model_validate({"plugins": {"trusted_publishers": {"acme": "not-a-key"}}})
    _, public_key = generate_private_key_pem()
    settings = AppSettings.model_validate(
        {"plugins": {"trusted_publishers": {"acme": public_key}, "require_signed": True}}
    )
    assert settings.plugins.trusted_publishers == {"acme": (public_key,)}


def test_workflow_hooks_need_a_signature_too(package: Package) -> None:
    (package.root / "hooks.py").write_text("def provider():\n    return None\n")
    settings = AppSettings.model_validate(
        {
            "security": {"trusted_plugin_prefixes": [package.name]},
            "plugins": {"trusted_publishers": {"acme": package.public_key}, "require_signed": True},
        }
    )
    manifest = type(
        "Manifest", (), {"name": "flow", "hook_provider": f"{package.name}.hooks:provider"}
    )
    service = WorkflowService(settings, container=None)
    assert "not signed" in (service._trusted(manifest) or "")  # type: ignore[arg-type]
    package.sign()
    assert WorkflowService(settings, container=None)._trusted(manifest) is None  # type: ignore[arg-type]


def test_cli_keygen_sign_and_verify(
    package: Package, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "signing-key.pem"
    with pytest.raises(SystemExit) as done:
        main(["plugin", "keygen", str(key_file)])
    assert done.value.code == 0
    assert key_file.stat().st_mode & 0o777 == 0o600
    public_key = capsys.readouterr().out.split("public key: ")[1].strip()

    with pytest.raises(SystemExit) as done:
        main(
            [
                *("plugin", "sign", str(package.root), "--publisher", "acme"),
                *("--key", str(key_file), "--name", "crm", "--version", "1.0.0"),
            ]
        )
    assert done.value.code == 0

    config = tmp_path / "agent.yaml"
    config.write_text(
        json.dumps({"plugins": {"trusted_publishers": {"acme": public_key}}}), encoding="utf-8"
    )
    with pytest.raises(SystemExit) as done:
        main(["plugin", "verify", package.reference, "--config", str(config)])
    assert done.value.code == 0
    assert "signed by acme" in capsys.readouterr().out
    assert not package.marker.exists()

    (package.root / "plugin.py").write_text("EVIL = True\n")
    with pytest.raises(SystemExit) as done:
        main(["plugin", "verify", package.reference, "--config", str(config)])
    assert done.value.code == 1


def test_verification_without_cryptography_says_what_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for module in [m for m in sys.modules if m.split(".")[0] == "cryptography"]:
        monkeypatch.setitem(sys.modules, module, None)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.exceptions", None)
    name = f"signedplug_{uuid.uuid4().hex[:8]}"
    (tmp_path / name).mkdir()
    (tmp_path / name / "__init__.py").write_text("")
    (tmp_path / name / SIGNATURE_FILE).write_text(
        json.dumps(
            {
                "format": "algen-plugin-signature/v1",
                "publisher": "acme",
                "package": name,
                "files": {"__init__.py": ""},
                "signature": "",
            }
        )
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    settings = PluginSettings(trusted_publishers={"acme": "A" * 43 + "="}, require_signed=True)
    with pytest.raises(ConfigurationError, match=r"algen-agent-runtime\[encryption\]"):
        verify_module(name, settings)
