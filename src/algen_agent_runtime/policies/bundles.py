"""Policy bundles: declarative policy rules distributed as a file with a detached signature.

A bundle is one YAML or JSON document. Its metadata (``name``, ``version``, ``issued_at``,
``expires_at``) is inside the signed bytes, so an expired bundle, or an older signed bundle put
back in place of a newer one, is refused at load (``expires_at`` and ``min_version``). The
signature is Ed25519 over the bundle file's exact bytes, base64-encoded in a detached file
(``<bundle>.sig`` unless named otherwise), and verified against public keys the deployment
trusts.

Key references are resolved, and the bundle read, before ``cryptography`` (the ``encryption``
extra) is imported; an unsigned bundle needs no extra at all.

Each loaded bundle becomes one policy, ``bundle:<name>``, that the policy engine runs for every
agent (it is mandatory, like authorization), whatever the agent's guardrail selection.
"""

from __future__ import annotations

import base64
import binascii
import fnmatch
import hashlib
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from algen_agent_runtime.exceptions.errors import ConfigurationError
from algen_agent_runtime.policies.contracts import PolicyAction, PolicyDecision, PolicyPoint

if TYPE_CHECKING:
    from algen_agent_runtime.config.settings import PolicySettings

_logger = structlog.get_logger("algen_agent_runtime.policies")

SIGNATURE_SUFFIX = ".sig"
#: Clock skew allowed between the signer and this host for ``issued_at``.
ISSUED_AT_SKEW = timedelta(minutes=5)
_INSTALL_HINT = "install algen-agent-runtime[encryption] to sign or verify policy bundles"
_CONTENT_POINTS = tuple(
    point
    for point in PolicyPoint
    if point not in {PolicyPoint.AFTER_MEMORY_WRITE, PolicyPoint.ATTACHMENTS}
)
_TOOL_RULE_POINTS = frozenset({PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL})


class PatternRule(BaseModel):
    """A regular expression checked against every string of the payload at its boundaries."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=128)
    pattern: str = Field(min_length=1, max_length=2_000)
    boundaries: tuple[PolicyPoint, ...] = _CONTENT_POINTS
    replacement: str = Field(default="[REDACTED]", max_length=200)

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"pattern does not compile: {exc}") from exc
        return value

    @field_validator("boundaries")
    @classmethod
    def _content_boundaries(cls, value: tuple[PolicyPoint, ...]) -> tuple[PolicyPoint, ...]:
        if not value:
            raise ValueError("a pattern rule needs at least one boundary")
        if PolicyPoint.AFTER_MEMORY_WRITE in value:
            raise ValueError("after_memory_write carries no content to match")
        return value


class PolicyBundleRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    #: Tool names (``fnmatch`` globs) refused at planning and before they run.
    deny_tools: tuple[str, ...] = ()
    #: Tool names (globs) that need a human approval before they run, whatever their side effect.
    require_approval_tools: tuple[str, ...] = ()
    #: Patterns that refuse the payload at their boundaries.
    deny_patterns: tuple[PatternRule, ...] = ()
    #: Patterns replaced at their boundaries. At ``before_tool`` a changed argument refuses the
    #: call, as every redaction of tool arguments does.
    redact_patterns: tuple[PatternRule, ...] = ()


class PolicyBundle(BaseModel):
    """The signed document. Everything here, metadata included, is covered by the signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal[1] = 1
    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version: int = Field(ge=1)
    issued_at: datetime
    expires_at: datetime | None = None
    description: str = Field(default="", max_length=2_000)
    rules: PolicyBundleRules = Field(default_factory=PolicyBundleRules)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("timestamps need a timezone, for example 2026-10-01T00:00:00Z")
        return value

    @model_validator(mode="after")
    def _expiry_after_issue(self) -> PolicyBundle:
        if self.expires_at is not None and self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        return self


@dataclass(frozen=True)
class LoadedPolicyBundle:
    bundle: PolicyBundle
    path: str
    sha256: str
    signed: bool
    key_id: str | None = None


def key_id(public_key: bytes) -> str:
    """A short identifier of a raw Ed25519 public key, safe to log."""
    return hashlib.sha256(public_key).hexdigest()[:16]


def resolve_key_reference(
    reference: str, environment: Mapping[str, str] | None = None, *, name: str
) -> str:
    """The text an ``env://VARIABLE`` or ``file://path`` key reference points at.

    Never logs or echoes the value. Literal keys in configuration are refused.
    """
    if reference.startswith("env://"):
        variable = reference.removeprefix("env://")
        value = (environment or {}).get(variable) or os.getenv(variable)
        if not value:
            raise ConfigurationError(f"{name} environment variable {variable!r} is not set")
        return value
    if reference.startswith("file://"):
        path = Path(reference.removeprefix("file://"))
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"{name} file {str(path)!r} cannot be read") from exc
    raise ConfigurationError(f"{name} must be an env:// or file:// reference")


def _crypto() -> Any:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:
        raise ImportError(_INSTALL_HINT) from exc
    return serialization, ed25519


def _raw_key(text: str, name: str) -> bytes | None:
    """A raw 32-byte key from base64 text, or None when the text is PEM."""
    stripped = text.strip()
    if stripped.startswith("-----BEGIN"):
        return None
    try:
        raw = base64.b64decode(stripped + "=" * (-len(stripped) % 4), altchars=b"-_")
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(f"{name} must be PEM or a base64 32-byte Ed25519 key") from exc
    if len(raw) != 32:
        raise ConfigurationError(f"{name} must be PEM or a base64 32-byte Ed25519 key")
    return raw


def _public_key(text: str, name: str) -> Any:
    serialization, ed25519 = _crypto()
    raw = _raw_key(text, name)
    if raw is not None:
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    try:
        key = serialization.load_pem_public_key(text.strip().encode())
    except ValueError as exc:
        raise ConfigurationError(f"{name} is not a readable public key") from exc
    if not isinstance(key, ed25519.Ed25519PublicKey):
        raise ConfigurationError(f"{name} is not an Ed25519 public key")
    return key


def _private_key(text: str, name: str) -> Any:
    serialization, ed25519 = _crypto()
    raw = _raw_key(text, name)
    if raw is not None:
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)
    try:
        key = serialization.load_pem_private_key(text.strip().encode(), password=None)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} is not a readable unencrypted private key") from exc
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ConfigurationError(f"{name} is not an Ed25519 private key")
    return key


def _raw_public_bytes(key: Any) -> bytes:
    serialization, _ = _crypto()
    raw: bytes = key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return raw


def generate_signing_key() -> tuple[str, str]:
    """A new Ed25519 key pair as (private PKCS#8 PEM, public SubjectPublicKeyInfo PEM)."""
    serialization, ed25519 = _crypto()
    private = ed25519.Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def sign_policy_bundle(data: bytes, private_key: str) -> str:
    """The detached signature of a bundle file's bytes, as the ``.sig`` file holds it.

    ``private_key`` is the key's text (PEM or base64 raw seed), never a reference.
    """
    parse_policy_bundle(data, "bundle")  # Never sign something that will not load.
    key = _private_key(private_key, "signing key")
    return base64.b64encode(key.sign(data)).decode("ascii")


def parse_policy_bundle(data: bytes, source: str) -> PolicyBundle:
    try:
        document = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"policy bundle {source!r} is not valid YAML or JSON") from exc
    if not isinstance(document, Mapping):
        raise ConfigurationError(f"policy bundle {source!r} must be a mapping")
    try:
        return PolicyBundle.model_validate(document)
    except ValidationError as exc:
        raise ConfigurationError(f"policy bundle {source!r} is invalid: {exc}") from exc


def _verify(
    data: bytes, signature_text: str, trusted: Sequence[tuple[str, str]], source: str
) -> str:
    """The id of the trusted key that signed ``data``; raises when none did."""
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(f"policy bundle {source!r} signature is not base64") from exc
    keys = [(_public_key(text, name), name) for name, text in trusted]
    for key, _ in keys:
        try:
            key.verify(signature, data)
        except Exception:
            continue
        return key_id(_raw_public_bytes(key))
    raise ConfigurationError(
        f"policy bundle {source!r} signature does not verify against any trusted key"
    )


def load_policy_bundle(
    path: str | Path,
    *,
    signature: str | Path | None = None,
    trusted_keys: Sequence[str] = (),
    require_signed: bool = False,
    min_version: int | None = None,
    environment: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> LoadedPolicyBundle:
    """Read, verify and validate one bundle. Raises ``ConfigurationError`` on any refusal.

    A bundle with a signature file is verified whatever ``require_signed`` says: a signature
    that does not verify is never ignored. Without one, the bundle loads only when
    ``require_signed`` is false.
    """
    source = str(path)
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"policy bundle {source!r} cannot be read") from exc
    signature_path = Path(signature) if signature is not None else Path(source + SIGNATURE_SUFFIX)
    signature_text: str | None = None
    if signature_path.exists():
        try:
            signature_text = signature_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigurationError(f"policy bundle {source!r} signature cannot be read") from exc
    elif signature is not None:
        raise ConfigurationError(f"policy bundle {source!r} signature file is missing")
    signer: str | None = None
    if signature_text is not None:
        if not trusted_keys:
            raise ConfigurationError(
                f"policy bundle {source!r} is signed but no trusted keys are configured"
            )
        # References resolve before cryptography is imported (AGENTS.md).
        trusted = [
            (
                f"trusted key {index}",
                resolve_key_reference(item, environment, name=f"trusted key {index}"),
            )
            for index, item in enumerate(trusted_keys)
        ]
        signer = _verify(data, signature_text, trusted, source)
    elif require_signed:
        raise ConfigurationError(
            f"policy bundle {source!r} is not signed and policies.require_signed is true"
        )
    bundle = parse_policy_bundle(data, source)
    current = now or datetime.now(UTC)
    if bundle.expires_at is not None and bundle.expires_at <= current:
        raise ConfigurationError(
            f"policy bundle {bundle.name!r} version {bundle.version} expired at "
            f"{bundle.expires_at.isoformat()}"
        )
    if bundle.issued_at > current + ISSUED_AT_SKEW:
        raise ConfigurationError(
            f"policy bundle {bundle.name!r} version {bundle.version} is issued in the future"
        )
    if min_version is not None and bundle.version < min_version:
        raise ConfigurationError(
            f"policy bundle {bundle.name!r} version {bundle.version} is older than the "
            f"minimum version {min_version}; refusing a rollback"
        )
    loaded = LoadedPolicyBundle(
        bundle=bundle,
        path=source,
        sha256=hashlib.sha256(data).hexdigest(),
        signed=signer is not None,
        key_id=signer,
    )
    log = _logger.info if loaded.signed else _logger.warning
    log(
        "policy_bundle_loaded",
        bundle=bundle.name,
        version=bundle.version,
        sha256=loaded.sha256,
        signed=loaded.signed,
        key_id=signer,
    )
    return loaded


def load_policy_bundles(
    settings: PolicySettings,
    environment: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> tuple[LoadedPolicyBundle, ...]:
    """Every bundle ``policies.bundles`` lists, verified; any refusal stops startup."""
    loaded: list[LoadedPolicyBundle] = []
    names: set[str] = set()
    for item in settings.bundles:
        bundle = load_policy_bundle(
            item.path,
            signature=item.signature,
            trusted_keys=item.trusted_keys or settings.trusted_keys,
            require_signed=settings.require_signed,
            min_version=item.min_version,
            environment=environment,
            now=now,
        )
        if bundle.bundle.name in names:
            raise ConfigurationError(f"policy bundle {bundle.bundle.name!r} is listed twice")
        names.add(bundle.bundle.name)
        loaded.append(bundle)
    return tuple(loaded)


def _map_strings(value: Any, transform: Callable[[str], str]) -> tuple[Any, bool]:
    """``value`` with ``transform`` applied to every string in it, and whether one changed."""
    if isinstance(value, Enum):
        return value, False
    if isinstance(value, str):
        changed = transform(value)
        return changed, changed != value
    if isinstance(value, BaseModel):
        updates: dict[str, Any] = {}
        any_changed = False
        for name in type(value).model_fields:
            item, item_changed = _map_strings(getattr(value, name), transform)
            updates[name] = item
            any_changed = any_changed or item_changed
        return (value.model_copy(update=updates) if any_changed else value), any_changed
    if isinstance(value, Mapping):
        mapping: dict[Any, Any] = {}
        any_changed = False
        for key, item in value.items():
            mapping[key], item_changed = _map_strings(item, transform)
            any_changed = any_changed or item_changed
        return (mapping if any_changed else value), any_changed
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = []
        any_changed = False
        for item in value:
            mapped, item_changed = _map_strings(item, transform)
            items.append(mapped)
            any_changed = any_changed or item_changed
        if not any_changed:
            return value, False
        return (tuple(items) if isinstance(value, tuple) else items), True
    return value, False


class BundlePolicy:
    """Enforces one bundle's rules. Registered as ``bundle:<name>`` and run for every agent."""

    category = "policy_bundle"
    enforcement_mode = "block"

    def __init__(self, loaded: LoadedPolicyBundle) -> None:
        self.loaded = loaded
        bundle = loaded.bundle
        rules = bundle.rules
        self.name = f"bundle:{bundle.name}"
        self._deny = [
            (rule, re.compile(rule.pattern), frozenset(rule.boundaries))
            for rule in rules.deny_patterns
        ]
        self._redact = [
            (rule, re.compile(rule.pattern), frozenset(rule.boundaries))
            for rule in rules.redact_patterns
        ]
        points: set[PolicyPoint] = set()
        if rules.deny_tools or rules.require_approval_tools:
            points.update(_TOOL_RULE_POINTS)
        for _, _, boundaries in (*self._deny, *self._redact):
            points.update(boundaries)
        self.applies_to = frozenset(points)
        self._metadata = {"bundle": bundle.name, "bundle_version": bundle.version}

    def _decision(
        self, action: PolicyAction, reason_code: str, reason: str, **metadata: Any
    ) -> PolicyDecision:
        return PolicyDecision(
            action=action,
            reason_code=reason_code,
            reason=reason,
            audit_metadata={**self._metadata, **metadata},
        )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        boundary = PolicyPoint(point)
        rules = self.loaded.bundle.rules
        tool = context.get("tool")
        tool_name = getattr(tool, "name", None)
        if boundary in _TOOL_RULE_POINTS and isinstance(tool_name, str):
            for glob in rules.deny_tools:
                if fnmatch.fnmatchcase(tool_name, glob):
                    return self._decision(
                        PolicyAction.DENY,
                        "bundle.tool_denied",
                        f"Tool {tool_name} is not allowed by policy bundle "
                        f"{self.loaded.bundle.name}.",
                        rule=glob,
                    )
            if boundary == PolicyPoint.PLAN_TOOL:
                for glob in rules.require_approval_tools:
                    if fnmatch.fnmatchcase(tool_name, glob):
                        return self._decision(
                            PolicyAction.REQUIRE_APPROVAL,
                            "bundle.approval_required",
                            f"Policy bundle {self.loaded.bundle.name} requires approval for "
                            f"{tool_name}.",
                            rule=glob,
                            risk=getattr(getattr(tool, "side_effect", None), "value", "policy"),
                        )
        for rule, pattern, boundaries in self._deny:
            if boundary not in boundaries:
                continue
            found = False

            def search(text: str, pattern: re.Pattern[str] = pattern) -> str:
                nonlocal found
                found = found or pattern.search(text) is not None
                return text

            _map_strings(payload, search)
            if found:
                return self._decision(
                    PolicyAction.DENY,
                    "bundle.pattern_denied",
                    f"The content matches rule {rule.name} of policy bundle "
                    f"{self.loaded.bundle.name}.",
                    rule=rule.name,
                )
        transformed = payload
        applied: list[str] = []
        for rule, pattern, boundaries in self._redact:
            if boundary not in boundaries:
                continue

            def replace(
                text: str, pattern: re.Pattern[str] = pattern, replacement: str = rule.replacement
            ) -> str:
                return pattern.sub(replacement, text)

            transformed, changed = _map_strings(transformed, replace)
            if changed:
                applied.append(rule.name)
        if applied:
            return PolicyDecision(
                action=PolicyAction.REDACT,
                reason_code="bundle.redacted",
                reason=f"Content was redacted by policy bundle {self.loaded.bundle.name}.",
                value=transformed,
                audit_metadata={**self._metadata, "rules": applied},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="bundle.allowed",
            reason="No bundle rule applies.",
            audit_metadata=dict(self._metadata),
        )
