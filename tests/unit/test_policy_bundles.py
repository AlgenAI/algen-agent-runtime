"""Signed policy bundles: verified at load against trusted Ed25519 keys, with expiry and rollback
checks on the signed metadata, and enforced for every agent once loaded.

Keys are generated in the tests; no private key is committed.
"""

from __future__ import annotations

import base64
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import make_agent, make_runtime

from algen_agent_runtime.cli import main
from algen_agent_runtime.config.settings import AppSettings, PolicyBundleSettings, PolicySettings
from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.models.providers.mock import MockModelProvider, tool_call_response
from algen_agent_runtime.orchestration.container import build_container
from algen_agent_runtime.policies.bundles import (
    BundlePolicy,
    load_policy_bundle,
    load_policy_bundles,
    sign_policy_bundle,
)
from algen_agent_runtime.policies.contracts import PolicyAction
from algen_agent_runtime.policies.engine import CompositePolicyEngine
from algen_agent_runtime.tools.contracts import SideEffect, Tool, ToolContext, ToolDefinition
from algen_agent_runtime.types.contracts import (
    ApprovalPolicy,
    GuardrailPolicy,
    RunRequest,
    RunStatus,
)

NOW = datetime(2026, 10, 29, tzinfo=UTC)


def _document(**updates: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "name": "acme-baseline",
        "version": 3,
        "issued_at": "2026-10-01T00:00:00Z",
        "expires_at": "2099-01-01T00:00:00Z",
        "rules": {
            "deny_tools": ["payments.*"],
            "require_approval_tools": ["lookup"],
            "deny_patterns": [
                {"name": "codename", "pattern": "(?i)project nightingale", "boundaries": ["input"]}
            ],
            "redact_patterns": [
                {
                    "name": "employee-id",
                    "pattern": r"EMP-\d{6}",
                    "replacement": "[EMPLOYEE]",
                    "boundaries": ["after_tool", "final_response"],
                }
            ],
        },
    }
    document.update(updates)
    return document


def _write(tmp_path: Path, name: str = "bundle.yaml", **updates: Any) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(_document(**updates)), encoding="utf-8")
    return path


@pytest.fixture
def keys() -> tuple[str, str]:
    pytest.importorskip("cryptography")
    from algen_agent_runtime.policies.bundles import generate_signing_key

    return generate_signing_key()


def _sign(path: Path, private_pem: str) -> Path:
    signature = path.with_name(path.name + ".sig")
    signature.write_text(sign_policy_bundle(path.read_bytes(), private_pem), encoding="ascii")
    return signature


def _env(public_pem: str) -> dict[str, str]:
    return {"POLICY_KEY": public_pem}


def test_a_signed_bundle_loads_with_its_signer(tmp_path: Path, keys: tuple[str, str]) -> None:
    private, public = keys
    path = _write(tmp_path)
    _sign(path, private)

    loaded = load_policy_bundle(
        path,
        trusted_keys=("env://POLICY_KEY",),
        require_signed=True,
        environment=_env(public),
        now=NOW,
    )

    assert loaded.signed and loaded.key_id and len(loaded.sha256) == 64
    assert (loaded.bundle.name, loaded.bundle.version) == ("acme-baseline", 3)


def test_a_raw_base64_key_from_a_file_verifies(tmp_path: Path, keys: tuple[str, str]) -> None:
    from cryptography.hazmat.primitives import serialization

    private, public = keys
    key = serialization.load_pem_public_key(public.encode())
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_file = tmp_path / "trusted.key"
    key_file.write_text(base64.b64encode(raw).decode())
    path = _write(tmp_path)
    _sign(path, private)

    loaded = load_policy_bundle(path, trusted_keys=(f"file://{key_file}",), now=NOW)

    assert loaded.signed


@pytest.mark.parametrize(
    "tamper",
    ["edit", "other_key", "garbage"],
)
def test_a_signature_that_does_not_verify_is_refused_even_when_not_required(
    tmp_path: Path, keys: tuple[str, str], tamper: str
) -> None:
    from algen_agent_runtime.policies.bundles import generate_signing_key

    private, public = keys
    path = _write(tmp_path)
    signature = _sign(path, private)
    if tamper == "edit":
        path.write_text(path.read_text().replace("version: 3", "version: 4"))
    elif tamper == "other_key":
        _sign(path, generate_signing_key()[0])
    else:
        signature.write_text("not base64!")

    with pytest.raises(ConfigurationError, match="signature"):
        load_policy_bundle(
            path, trusted_keys=("env://POLICY_KEY",), environment=_env(public), now=NOW
        )


def test_require_signed_refuses_an_unsigned_bundle(tmp_path: Path) -> None:
    path = _write(tmp_path)

    with pytest.raises(ConfigurationError, match="not signed"):
        load_policy_bundle(path, require_signed=True, now=NOW)


def test_an_unsigned_bundle_loads_without_cryptography_when_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "cryptography", None)
    path = _write(tmp_path)

    loaded = load_policy_bundle(path, now=NOW)

    assert not loaded.signed and loaded.key_id is None


def test_a_signed_bundle_without_the_extra_names_it_after_resolving_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path)
    (tmp_path / "bundle.yaml.sig").write_text("c2lnbmF0dXJl")
    monkeypatch.setitem(sys.modules, "cryptography", None)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", None)

    # A missing key reference is reported first, without importing the optional package.
    with pytest.raises(ConfigurationError, match="MISSING_KEY"):
        load_policy_bundle(path, trusted_keys=("env://MISSING_KEY",), environment={}, now=NOW)
    with pytest.raises(ImportError, match=r"algen-agent-runtime\[encryption\]"):
        load_policy_bundle(
            path, trusted_keys=("env://POLICY_KEY",), environment={"POLICY_KEY": "x"}, now=NOW
        )


def test_a_signed_bundle_needs_trusted_keys(tmp_path: Path) -> None:
    path = _write(tmp_path)
    (tmp_path / "bundle.yaml.sig").write_text("c2lnbmF0dXJl")

    with pytest.raises(ConfigurationError, match="no trusted keys"):
        load_policy_bundle(path, now=NOW)


def test_a_named_signature_file_must_exist(tmp_path: Path) -> None:
    path = _write(tmp_path)

    with pytest.raises(ConfigurationError, match="signature file is missing"):
        load_policy_bundle(path, signature=tmp_path / "absent.sig", now=NOW)


def test_an_expired_bundle_is_refused(tmp_path: Path, keys: tuple[str, str]) -> None:
    private, public = keys
    path = _write(tmp_path, expires_at="2026-10-15T00:00:00Z")
    _sign(path, private)

    with pytest.raises(ConfigurationError, match="expired"):
        load_policy_bundle(
            path, trusted_keys=("env://POLICY_KEY",), environment=_env(public), now=NOW
        )


def test_a_bundle_issued_in_the_future_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, issued_at=(NOW + timedelta(hours=1)).isoformat())

    with pytest.raises(ConfigurationError, match="future"):
        load_policy_bundle(path, now=NOW)


def test_rolling_back_to_an_older_signed_bundle_is_refused(
    tmp_path: Path, keys: tuple[str, str]
) -> None:
    private, public = keys
    path = _write(tmp_path, version=2)
    _sign(path, private)

    with pytest.raises(ConfigurationError, match="rollback"):
        load_policy_bundle(
            path,
            trusted_keys=("env://POLICY_KEY",),
            min_version=3,
            environment=_env(public),
            now=NOW,
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"version": 0},
        {"issued_at": "2026-10-01T00:00:00"},
        {"expires_at": "2026-09-01T00:00:00Z"},
        {"rules": {"deny_patterns": [{"name": "x", "pattern": "("}]}},
        {"rules": {"redact_patterns": [{"name": "x", "pattern": "a", "boundaries": []}]}},
        {"rules": {"unknown": []}},
    ],
)
def test_an_invalid_bundle_is_refused(tmp_path: Path, updates: dict[str, Any]) -> None:
    path = _write(tmp_path, **updates)

    with pytest.raises(ConfigurationError, match="invalid"):
        load_policy_bundle(path, now=NOW)


def test_settings_refuse_literal_keys() -> None:
    with pytest.raises(ValueError, match="never a literal key"):
        PolicySettings(trusted_keys=("MCowBQYDK2VwAyEA",))
    with pytest.raises(ValueError, match="never a literal key"):
        PolicyBundleSettings(path="b.yaml", trusted_keys=("-----BEGIN PUBLIC KEY-----",))


def test_the_same_bundle_twice_is_refused(tmp_path: Path) -> None:
    first = _write(tmp_path, "a.yaml")
    second = _write(tmp_path, "b.yaml", version=4)
    settings = PolicySettings(
        bundles=(PolicyBundleSettings(path=str(first)), PolicyBundleSettings(path=str(second)))
    )

    with pytest.raises(ConfigurationError, match="listed twice"):
        load_policy_bundles(settings, now=NOW)


def test_the_container_refuses_to_start_with_an_unsigned_bundle_when_signing_is_required(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path)
    settings = AppSettings(
        policies=PolicySettings(
            require_signed=True,
            trusted_keys=("env://POLICY_KEY",),
            bundles=(PolicyBundleSettings(path=str(path)),),
        )
    )

    with pytest.raises(ConfigurationError, match="not signed"):
        build_container(settings)


def test_the_container_registers_a_signed_bundle_for_every_agent(
    tmp_path: Path, keys: tuple[str, str]
) -> None:
    private, public = keys
    path = _write(tmp_path)
    _sign(path, private)
    settings = AppSettings(
        policies=PolicySettings(
            require_signed=True,
            trusted_keys=("env://POLICY_KEY",),
            bundles=(PolicyBundleSettings(path=str(path), min_version=3),),
        )
    )

    engine = build_container(settings, environment=_env(public)).runtime.policies

    assert "bundle:acme-baseline" in engine.list()
    assert "bundle:acme-baseline" in engine.mandatory()


def _tool(name: str, side_effect: SideEffect = SideEffect.READ) -> Tool:
    calls: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        calls.append(arguments)
        return {"employee": "EMP-123456"}

    tool = Tool(
        ToolDefinition(
            name=name,
            version="1",
            description=name,
            side_effect=side_effect,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
        handler,
    )
    tool.calls = calls  # type: ignore[attr-defined]
    return tool


def _runtime_with_bundle(tmp_path: Path, provider: MockModelProvider, *tools: Tool) -> Any:
    loaded = load_policy_bundle(_write(tmp_path), now=NOW)
    agent = make_agent(
        enabled_tools=frozenset(tool.definition.name for tool in tools),
        approval_policy=ApprovalPolicy(
            require_for_side_effects=False, require_for_destructive=False
        ),
        # The agent selects no policy at all; the bundle still applies.
        guardrail_policy=GuardrailPolicy(policies=()),
    )
    runtime = make_runtime(provider, agent)
    for tool in tools:
        runtime.tools.register(tool)
    engine = CompositePolicyEngine(audits=runtime.audits)
    engine.register(BundlePolicy(loaded), mandatory=True)
    runtime.policies = engine
    runtime.tool_executor._policy_engine = engine
    return runtime


async def test_a_bundle_denied_tool_is_refused_before_it_runs(tmp_path: Path) -> None:
    tool = _tool("payments.transfer", SideEffect.EXTERNAL)
    provider = MockModelProvider([tool_call_response("payments.transfer", {"amount": 10}), "ok"])
    runtime = _runtime_with_bundle(tmp_path, provider, tool)

    result = await runtime.run(
        RunRequest(agent="test-agent", input="pay", tenant_id="t", user_id="u")
    )

    assert result.status == RunStatus.FAILED
    assert tool.calls == []  # type: ignore[attr-defined]
    audits = await runtime.audits.list("t", "plan_tool")
    assert [(audit.outcome, audit.metadata["reason_code"]) for audit in audits] == [
        ("deny", "bundle.tool_denied")
    ]


async def test_a_bundle_can_require_approval_for_a_read_tool(tmp_path: Path) -> None:
    tool = _tool("lookup")
    provider = MockModelProvider([tool_call_response("lookup", {}), "ok"])
    runtime = _runtime_with_bundle(tmp_path, provider, tool)

    result = await runtime.run(
        RunRequest(agent="test-agent", input="go", tenant_id="t", user_id="u")
    )

    assert result.status == RunStatus.AWAITING_APPROVAL
    assert tool.calls == []  # type: ignore[attr-defined]


async def test_bundle_patterns_deny_and_redact_at_their_boundaries(tmp_path: Path) -> None:
    loaded = load_policy_bundle(_write(tmp_path), now=NOW)
    policy = BundlePolicy(loaded)

    denied = await policy.evaluate("input", "Tell me about Project Nightingale", {})
    redacted = await policy.evaluate("after_tool", {"rows": [{"id": "EMP-123456"}]}, {})
    elsewhere = await policy.evaluate("after_model", "EMP-123456", {})

    assert (denied.action, denied.reason_code) == (PolicyAction.DENY, "bundle.pattern_denied")
    assert denied.audit_metadata == {
        "bundle": "acme-baseline",
        "bundle_version": 3,
        "rule": "codename",
    }
    assert redacted.action == PolicyAction.REDACT
    assert redacted.value == {"rows": [{"id": "[EMPLOYEE]"}]}
    assert elsewhere.action == PolicyAction.ALLOW


def test_the_cli_generates_signs_and_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("cryptography")
    path = _write(tmp_path)
    private, public = tmp_path / "signing.pem", tmp_path / "signing.pub"

    def run(*argv: str) -> int:
        with pytest.raises(SystemExit) as exit_info:
            main(["policy", *argv])
        return int(exit_info.value.code or 0)

    assert run("keygen", "--private-key", str(private), "--public-key", str(public)) == 0
    assert private.stat().st_mode & 0o077 == 0
    assert run("keygen", "--private-key", str(private), "--public-key", str(public)) == 1
    assert run("sign", str(path), "--key", f"file://{private}") == 0
    assert (tmp_path / "bundle.yaml.sig").exists()
    assert run("verify", str(path), "--trusted-key", f"file://{public}") == 0
    assert '"version": 3' in capsys.readouterr().out
    assert run("verify", str(path), "--trusted-key", f"file://{public}", "--min-version", "4") == 1
    assert "rollback" in capsys.readouterr().err
    # The CLI never takes a literal key.
    assert run("sign", str(path), "--key", private.read_text()) == 1
