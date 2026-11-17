"""Providers, modes and inputs for the live provider contract suite (``tests/live``).

Modes:

- ``ALGEN_LIVE=1``: the real vendor APIs, each provider only when its credential (and, for
  Azure, endpoint and deployment) environment variables are set; Ollama only when its base URL
  answers. Keys are read through ``env://`` references by the provider classes themselves and
  are never printed, logged or stored by the suite.
- ``ALGEN_LIVE_FAKE=1``: every provider against the local fake API in ``live_fake_api.py``,
  with a placeholder key. It never reads a real key and never leaves 127.0.0.1; it takes
  precedence over ``ALGEN_LIVE``.
- neither: the whole suite is collected and skipped.

``ALGEN_LIVE_PROVIDERS=openai,anthropic`` narrows the run to some providers.
"""

from __future__ import annotations

import functools
import os
import struct
import zlib
from dataclasses import dataclass
from typing import Any

import httpx

from algen_agent_runtime.models.providers.adapters import (
    AnthropicProvider,
    AzureOpenAIProvider,
    DeepSeekProvider,
    GeminiProvider,
    GroqProvider,
    HuggingFaceInferenceProvider,
    MistralProvider,
    OllamaProvider,
    OpenAIProvider,
    XAIProvider,
)
from algen_agent_runtime.models.providers.openai_compatible import OpenAICompatibleProvider

FAKE = os.environ.get("ALGEN_LIVE_FAKE") == "1"
LIVE = os.environ.get("ALGEN_LIVE") == "1" and not FAKE
ENABLED = FAKE or LIVE
MODE = "fake" if FAKE else "live" if LIVE else "off"
SKIP_REASON = (
    "live provider contracts run with ALGEN_LIVE=1 (real APIs, keys from the environment) "
    "or ALGEN_LIVE_FAKE=1 (the local fake API)"
)

# A key that no vendor issues: the "wrong key" checks send it.
WRONG_KEY = "algen-live-wrong-key-0000000000000000"
# Small answers bound the cost of a run; reasoning models need some room to think.
MAX_OUTPUT_TOKENS = int(os.environ.get("ALGEN_LIVE_MAX_OUTPUT_TOKENS", "512"))
# A cancelled stream must close its response within this many seconds.
HANG_UP_SECONDS = 2.0
TOOL_NAME = "live.get_forecast"  # dotted: the wire alias mapping is part of the contract
FORECAST_CODE = "SUNNY-4217"
PDF_CODE = "ALGEN-PDF-7319"


@dataclass(frozen=True)
class Target:
    """One provider class as the suite drives it."""

    name: str  # test id, fake API prefix, and ALGEN_LIVE_<NAME>_* variables
    provider_type: str  # ``providers.<x>.type`` in agent.yaml
    key_env: str | None  # the credential variable (None: Ollama)
    default_model: str
    embed_model: str | None = None
    extra_env: tuple[str, ...] = ()

    @property
    def env_prefix(self) -> str:
        return f"ALGEN_LIVE_{self.name.upper()}"

    @property
    def model(self) -> str:
        if self.provider_type == "azure_openai" and LIVE:
            return os.environ.get("AZURE_OPENAI_DEPLOYMENT", "")
        return os.environ.get(f"{self.env_prefix}_MODEL", self.default_model)

    @property
    def embedding_model(self) -> str | None:
        if self.provider_type == "azure_openai":
            return os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT") or (
                "fake-embedding" if FAKE else None
            )
        return os.environ.get(f"{self.env_prefix}_EMBED_MODEL", self.embed_model or "") or None

    @property
    def key_reference(self) -> str | None:
        return f"env://{self.key_env}" if self.key_env else None

    @property
    def extensions(self) -> dict[str, Any]:
        # gpt-oss models think before they answer; keep that short so small limits suffice.
        if "gpt-oss" in self.model:
            return {"reasoning_effort": "low"}
        return {}


TARGETS: tuple[Target, ...] = (
    Target("openai", "openai", "OPENAI_API_KEY", "gpt-4o-mini", "text-embedding-3-small"),
    Target(
        "azure",
        "azure_openai",
        "AZURE_OPENAI_API_KEY",
        "fake-deployment",
        extra_env=("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT"),
    ),
    Target("anthropic", "anthropic", "ANTHROPIC_API_KEY", "claude-haiku-4-5"),
    Target("gemini", "gemini", "GEMINI_API_KEY", "gemini-2.5-flash-lite", "gemini-embedding-001"),
    Target("xai", "xai", "XAI_API_KEY", "grok-4-fast-non-reasoning"),
    Target("groq", "groq", "GROQ_API_KEY", "openai/gpt-oss-20b"),
    Target("mistral", "mistral", "MISTRAL_API_KEY", "mistral-small-latest", "mistral-embed"),
    Target("deepseek", "deepseek", "DEEPSEEK_API_KEY", "deepseek-chat"),
    Target("huggingface", "huggingface_inference", "HF_TOKEN", "openai/gpt-oss-20b"),
    Target("ollama", "ollama", None, "llama3.2", "nomic-embed-text"),
)
BY_NAME = {target.name: target for target in TARGETS}


def selected() -> tuple[Target, ...]:
    wanted = {
        name.strip()
        for name in os.environ.get("ALGEN_LIVE_PROVIDERS", "").split(",")
        if name.strip()
    }
    return tuple(target for target in TARGETS if not wanted or target.name in wanted)


def ollama_base_url() -> str:
    return os.environ.get("ALGEN_LIVE_OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")


def azure_api_version() -> str:
    return os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")


@functools.cache
def _ollama_answers(base_url: str) -> bool:
    try:
        httpx.get(base_url.rstrip("/") + "/models", timeout=1.0)
    except httpx.HTTPError:
        return False
    return True


def unavailable(target: Target) -> str | None:
    """Why ``target`` cannot run in this mode, or None when it can."""
    if not ENABLED:
        return SKIP_REASON
    if FAKE:
        return None
    if target.provider_type == "ollama":
        if not _ollama_answers(ollama_base_url()):
            return f"Ollama does not answer at {ollama_base_url()} (ALGEN_LIVE_OLLAMA_BASE_URL)"
        return None
    missing = [
        name
        for name in (target.key_env or "", *target.extra_env)
        if name and not os.environ.get(name)
    ]
    if missing:
        return f"{target.name}: set {', '.join(missing)} to run it"
    return None


class LiveSecrets:
    """Resolves ``env://`` references: from the environment live, to the placeholder in fake
    mode. Values are returned to the provider class only; nothing here prints them."""

    async def get(self, reference: str) -> str:
        if FAKE:
            from live_fake_api import FAKE_KEY

            return FAKE_KEY
        variable = reference.removeprefix("env://")
        value = os.environ.get(variable)
        if not value:
            raise LookupError(f"environment variable {variable} is not set")
        return value


class WrongKeySecrets:
    """Every reference resolves to a key no vendor issued."""

    async def get(self, reference: str) -> str:
        return WRONG_KEY


def build_provider(
    target: Target,
    *,
    base_url: str | None,
    client: httpx.AsyncClient,
    secrets: Any,
    model: str | None = None,
) -> OpenAICompatibleProvider:
    """The Runtime's own provider class for ``target``; ``base_url`` None keeps its default."""
    model = model or target.model
    common: dict[str, Any] = {"secret_provider": secrets, "client": client}
    if base_url is not None:
        common["base_url"] = base_url
    key = target.key_reference or ""
    match target.provider_type:
        case "openai":
            return OpenAIProvider(key, model, **common)
        case "azure_openai":
            return AzureOpenAIProvider(
                base_url or os.environ.get("AZURE_OPENAI_ENDPOINT", ""),
                key,
                model,
                azure_api_version(),
                secret_provider=secrets,
                client=client,
            )
        case "anthropic":
            return AnthropicProvider(key, model, **common)
        case "deepseek":
            return DeepSeekProvider(key, model, **common)
        case "mistral":
            return MistralProvider(key, model, **common)
        case "gemini":
            return GeminiProvider(key, model, **common)
        case "xai":
            return XAIProvider(key, model, **common)
        case "groq":
            return GroqProvider(key, model, **common)
        case "huggingface_inference":
            return HuggingFaceInferenceProvider(key, model, **common)
        case "ollama":
            return OllamaProvider(model, base_url=base_url or ollama_base_url(), client=client)
    raise ValueError(target.provider_type)


def red_png(size: int = 32) -> bytes:
    """A ``size`` x ``size`` PNG filled with pure red."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def code_pdf(text: str = PDF_CODE) -> bytes:
    """A one-page PDF that shows ``text`` in large Helvetica."""
    stream = f"BT /F1 36 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
