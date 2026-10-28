"""Authenticated encryption for secrets the Runtime keeps at rest, such as MCP OAuth tokens.

``SecretCipher`` is the extension point: a deployment can plug in a KMS-backed cipher.
``AesGcmCipher`` is the built-in one: AES-256-GCM with a random 96-bit nonce per value and
associated data that binds each ciphertext to the row it belongs to, so a value copied to
another tenant's row fails to decrypt. It needs the ``encryption`` extra (``cryptography``).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from algen_agent_runtime.exceptions.errors import ConfigurationError

_FORMAT = "v1"


class SecretCipher(Protocol):
    """Encrypts and decrypts small secrets bound to ``associated_data``."""

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> str: ...

    def decrypt(self, ciphertext: str, associated_data: bytes) -> bytes: ...


class DecryptionError(ConfigurationError):
    """A stored secret could not be decrypted: wrong key, wrong row, or tampering."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key_id(key: bytes) -> str:
    # A short, non-reversible identifier so rotated keys can still decrypt older rows.
    return hashlib.sha256(b"algen-agent-runtime-key-id" + key).hexdigest()[:12]


def decode_key(value: str, name: str) -> bytes:
    """A 32-byte AES key from its URL-safe or standard base64 text."""
    try:
        key = _unb64(value.strip().replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a base64-encoded 32-byte key") from exc
    if len(key) != 32:
        raise ConfigurationError(f"{name} must be a base64-encoded 32-byte key")
    return key


def resolve_key_reference(
    reference: str, environment: Mapping[str, str] | None = None, *, name: str
) -> bytes:
    """Resolve an ``env://`` key reference and decode it. Never logs or echoes the key."""
    if not reference.startswith("env://"):
        raise ConfigurationError(f"{name} must be an env:// reference")
    variable = reference.removeprefix("env://")
    value = (environment or {}).get(variable) or os.getenv(variable)
    if not value:
        raise ConfigurationError(f"{name} environment variable {variable!r} is not set")
    return decode_key(value, name)


def generate_key() -> str:
    """A new random key in the text form ``AesGcmCipher`` reads."""
    return _b64(secrets.token_bytes(32))


class AesGcmCipher:
    """AES-256-GCM. The first key encrypts; every key, including older ones, can decrypt."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise ConfigurationError("AesGcmCipher needs at least one key")
        for key in keys:
            if len(key) != 32:
                raise ConfigurationError("AesGcmCipher keys must be 32 bytes")
        # Keys are validated before the optional package is imported (AGENTS.md).
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise ImportError("install algen-agent-runtime[encryption]") from exc
        self._primary = _key_id(keys[0])
        self._ciphers: dict[str, Any] = {_key_id(key): AESGCM(key) for key in keys}

    @classmethod
    def from_references(
        cls,
        reference: str,
        previous: Sequence[str] = (),
        environment: Mapping[str, str] | None = None,
    ) -> AesGcmCipher:
        keys = [resolve_key_reference(reference, environment, name="encryption key")]
        keys.extend(
            resolve_key_reference(item, environment, name="previous encryption key")
            for item in previous
        )
        return cls(keys)

    def __repr__(self) -> str:
        return f"AesGcmCipher(primary={self._primary!r}, keys={len(self._ciphers)})"

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> str:
        nonce = secrets.token_bytes(12)
        sealed = self._ciphers[self._primary].encrypt(nonce, plaintext, associated_data)
        return f"{_FORMAT}.{self._primary}.{_b64(nonce + sealed)}"

    def decrypt(self, ciphertext: str, associated_data: bytes) -> bytes:
        try:
            version, key_id, body = ciphertext.split(".", 2)
        except ValueError as exc:
            raise DecryptionError("stored secret has an unknown format") from exc
        cipher = self._ciphers.get(key_id)
        if version != _FORMAT or cipher is None:
            raise DecryptionError("stored secret was encrypted with an unknown key")
        try:
            raw = _unb64(body)
            plaintext: bytes = cipher.decrypt(raw[:12], raw[12:], associated_data)
        except Exception as exc:
            # cryptography raises InvalidTag; say nothing about the contents.
            raise DecryptionError("stored secret failed authentication") from exc
        return plaintext
