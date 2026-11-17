from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Any

# Kept for compatibility: the exact names the Runtime once redacted on. The rule is now
# ``is_sensitive_key``, which covers these and any key that contains a credential word.
SENSITIVE_KEYS = frozenset(
    {"authorization", "cookie", "password", "secret", "token", "api_key", "apikey", "credential"}
)
APPROVAL_SENSITIVE_KEYS = frozenset({"password", "secret", "token", "api_key", "authorization"})

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------------------------
# Text folding: what a detector matches on, so zero-width characters, fullwidth forms and Latin
# lookalikes from Cyrillic or Greek cannot hide a credential, an identifier or an instruction.

# Cyrillic and Greek letters that look like Latin ones, and the Latin letter each folds to.
_LOOKALIKES = (
    "\u0430\u0432\u0435\u0455\u0456\u0458\u043a\u043c\u043d\u043e\u0440\u0441"
    "\u0442\u0443\u0445\u0501\u04cf\u051b\u051d\u0410\u0412\u0415\u0405\u0406"
    "\u0408\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0423\u0425\u03b1\u03b5"
    "\u03b9\u03ba\u03bd\u03bf\u03c1\u03c4\u03c5\u03c7\u0391\u0392\u0395\u0396"
    "\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7"
)
_LATIN = "abesijkmhopctyxdlqwABESIJKMHOPCTYXaeikvoptuxABEZHIKMNOPTYX"
_CONFUSABLES = str.maketrans(_LOOKALIKES, _LATIN)


def fold_text(text: str) -> str:
    """``text`` as the detectors read it: NFKC-normalised (fullwidth and other compatibility
    forms become ASCII), with format characters (Unicode category ``Cf``: zero-width spaces and
    joiners, bidi controls, soft hyphens) removed and Cyrillic and Greek letters that look like
    Latin ones replaced by those. ASCII text is returned as it is.

    The secrets, PII and prompt-injection guardrails match on the original text and on this
    folding; a match found only in the folding cannot be mapped back to a span, so the whole
    value is redacted (or the input refused) instead.
    """
    if text.isascii():
        return text
    normalized = unicodedata.normalize("NFKC", text)
    stripped = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    return stripped.translate(_CONFUSABLES)


# ---------------------------------------------------------------------------------------------
# Which keys name a credential.

# Searched in the key lower-cased with everything but letters and digits removed, so
# ``x-api-key``, ``OPENAI_API_KEY``, ``client.secret`` and ``refreshToken`` all match.
_KEY_WORDS = re.compile(
    r"token|secret|passw(?:or)?d|passphrase|apikey|accesskey|authorization|cookie|credential"
    r"|privatekey|signingkey|sessionkey|^session$|^pwd$|^auth$"
)
# Counts and settings that only mention tokens: ``max_tokens``, ``token_count``, ``tokenizer``,
# ``token_url``. Removed before the search, so ``secret_tokens`` still names a credential.
_KEY_NOT_SECRET = re.compile(
    r"tokens|token(?:count|limit|usage|budget|izer|type|estimate|index|url|endpoint)"
    r"|(?:max|min|num)token"
)


def is_sensitive_key(key: Any) -> bool:
    """Whether a mapping key names a credential, whatever its case or separators.

    True for ``token``, ``access_token``, ``refreshToken``, ``client_secret``, ``x-api-key``,
    ``OPENAI_API_KEY``, ``Api-Key``, ``private_key``, ``password``, ``passwd``,
    ``authorization``, ``cookie``, ``credentials`` and ``session``; false for token counts and
    settings such as ``max_tokens``, ``input_tokens``, ``token_count`` and ``tokenizer``, and
    for ``session_id``.
    """
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"[^a-z0-9]", "", fold_text(key).lower())
    return _KEY_WORDS.search(_KEY_NOT_SECRET.sub("-", normalized)) is not None


# ---------------------------------------------------------------------------------------------
# Credentials in text.

# A key written in text that names a credential: the whole key or its last part (``secret``,
# ``SECRET_KEY``, ``db.password``, ``githubToken``, ``x-api-key``); ``max_tokens`` is not.
_SECRET_KEY = (
    r"[A-Za-z0-9_.-]*?"
    r"(?:api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|password|passwd|passphrase"
    r"|private[_-]?key|credentials?|token)"
)
# Keys that are also ordinary words: after one of these, ``key: value`` may be prose.
_PROSE_SECRET_KEYS = frozenset(
    {"secret", "password", "passwd", "passphrase", "token", "credential", "credentials"}
)
_KEYED = re.compile(
    r"(?i)(?P<kq>[\"']?)(?<![A-Za-z0-9_.-])(?P<key>" + _SECRET_KEY + r")(?![A-Za-z0-9_])"
    r"(?P=kq)\s*(?P<op>[:=])\s*(?P<vq>[\"']?)(?P<value>[A-Za-z0-9_\-/.+=~]{8,})"
)
# ``"api_key": "anything"`` (JSON, YAML, Python): a quoted value is a value, whatever it holds.
_KEYED_QUOTED = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?P<kq>[\"']?)(?P<key>" + _SECRET_KEY + r")(?![A-Za-z0-9_])"
    r"(?P=kq)\s*[:=]\s*(?P<vq>[\"'])(?P<value>(?:(?!(?P=vq))[^\\\n]|\\.){1,2048}?)(?P=vq)"
)
# ``Authorization: Bearer <token>``: a token has a digit or a separator, so prose such as
# "bearer instruments" is left alone.
_BEARER = re.compile(r"(?i)\bbearer\s+((?=[A-Za-z0-9._~+/-]*[0-9._~+/-])[A-Za-z0-9._~+/-]{6,}=*)")
# ``Authorization: Basic dXNlcjpwYXNz`` and the other schemes.
_AUTHORIZATION = re.compile(
    r"(?i)\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:basic|digest|token|negotiate)\s+"
    r"(?P<value>[A-Za-z0-9._~+/=-]{4,})"
)
# ``postgresql://user:password@host``: the password in a URL's user information.
_URL_PASSWORD = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]{1,30}://)(?P<user>[^\s:/?#@\"'<>\[\]]*):"
    r"(?P<value>[^\s@/?#\"'<>\[\]]+)@"
)
# An AWS secret access key written after its name in prose: "secret key wJalrXUtnFEMI/K7...".
_AWS_SECRET = re.compile(
    r"(?i)\bsecret(?:[ _-]access)?[ _-]key\b[\s:=\"']{1,4}(?P<value>[A-Za-z0-9/+]{30,64})(?![A-Za-z0-9/+])"
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?(?:-----END (?P=kind)-----|\Z)",
    re.DOTALL,
)
# Credentials recognisable by their shape alone.
SECRET_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI, Anthropic and compatible keys: sk-..., sk-proj-..., sk-ant-...
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    # AWS access key ids.
    re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{16}\b"),
    # GitHub tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"),
    # Slack tokens.
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    # Google API keys.
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    # JSON Web Tokens.
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)


def _looks_like_credential(value: str) -> bool:
    """A value written as prose after ``secret:`` or ``token:`` is redacted only when it reads
    as a credential: it has a digit, letters in mixed case inside it, or it is long and varied.
    ``The secret: consistency`` is left alone."""
    if any(character.isdigit() for character in value):
        return True
    if len(value) >= 12 and re.search(r"[a-z][A-Z]|[A-Z]{2}[a-z]", value):
        return True
    return len(value) >= 16 and len(set(value)) >= 10


def _keyed_is_secret(match: re.Match[str]) -> bool:
    key = match.group("key")
    return bool(
        match.group("op") == "="
        or match.group("kq")
        or match.group("vq")
        # An identifier (api_key, SECRET, githubToken), not a word in a sentence.
        or key.lower() not in _PROSE_SECRET_KEYS
        or key.isupper()
        or _looks_like_credential(match.group("value"))
    )


def _redact_ascii(text: str) -> tuple[str, int]:
    count = 0

    def group(name: str | int, check: Any = None) -> Any:
        def replace(match: re.Match[str]) -> str:
            nonlocal count
            if check is not None and not check(match):
                return match.group(0)
            if match.group(name) == REDACTED:
                return match.group(0)
            count += 1
            start, end = match.span(name)
            offset = match.start()
            whole = match.group(0)
            return whole[: start - offset] + REDACTED + whole[end - offset :]

        return replace

    def whole(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return REDACTED

    text = _PEM_PRIVATE_KEY.sub(whole, text)
    text = _URL_PASSWORD.sub(group("value"), text)
    text = _KEYED_QUOTED.sub(group("value"), text)
    text = _KEYED.sub(group("value", _keyed_is_secret), text)
    text = _AUTHORIZATION.sub(group("value"), text)
    text = _AWS_SECRET.sub(group("value"), text)
    text = _BEARER.sub(group(1), text)
    for pattern in SECRET_TOKEN_PATTERNS:
        text = pattern.sub(whole, text)
    return text, count


def redact_secret_text(text: str) -> tuple[str, int]:
    """``text`` with the credentials in it masked as ``[REDACTED]``, and how many were.

    Masks the value of a credential written as code or configuration (``api_key=...``,
    ``"password": "..."``, ``SECRET: ...``, ``x-api-key: ...``), bearer and other
    ``Authorization`` credentials, the password in a URL (``postgresql://user:pw@host``), PEM
    private keys, and tokens recognisable by shape (``sk-...``, AWS ``AKIA...``, GitHub
    ``ghp_...``/``github_pat_...``, Slack ``xox?-...``, Google ``AIza...``, JWTs). Prose such as
    "The secret: consistency" is left alone.

    Text that hides a credential behind zero-width characters, fullwidth forms or lookalike
    letters (see ``fold_text``) is replaced whole by ``[REDACTED]``.
    """
    redacted, count = _redact_ascii(text)
    folded = fold_text(text)
    if folded != text:
        hidden = _redact_ascii(folded)[1]
        if hidden > count:
            return REDACTED, hidden
    return redacted, count


def redact_text(value: str) -> str:
    """``value`` with the credentials in it masked (see ``redact_secret_text``)."""
    return redact_secret_text(value)[0]


# ---------------------------------------------------------------------------------------------
# Values.


def redact(value: Any) -> Any:
    """``value`` with credentials masked, for logs, telemetry and stored metadata.

    In a dict (at any depth), the whole value of a key that names a credential
    (``is_sensitive_key``) becomes ``[REDACTED]``; in every string, credentials are masked as
    ``redact_text`` masks them.
    """
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: REDACTED if is_sensitive_key(key) else redact(item) for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


def redact_held(value: Any, held: Iterable[str]) -> Any:
    """``value`` with every credential in ``held`` masked as ``[REDACTED]`` wherever it
    appears in a string, in dicts, lists and tuples at any depth (since 0.2.0b5).

    For credentials the caller holds and sent somewhere (a token, an ``env://`` secret, an MCP
    OAuth token) that come back in a reply: they have no recognisable shape, so only knowing
    them masks them. Longer values are masked first; empty values are ignored. The input is
    never modified.
    """
    values = sorted({item for item in held if item}, key=len, reverse=True)
    if not values:
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, str):
            for credential in values:
                item = item.replace(credential, REDACTED)
            return item
        if isinstance(item, Mapping):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        return item

    return walk(value)


def redact_error_text(error: BaseException | str) -> str:
    """An error's text (``str(error)``) with credentials masked as ``redact_secret_text`` masks
    them and personal identifiers as ``redact_pii`` does (since 0.2.0b5).

    The Runtime shows an error this way wherever it leaves the run (API responses, events,
    audits, logs, spans, work-queue rows); a host showing or logging an exception it caught
    from the Runtime, a tool or a provider can do the same. The error's type is not added.
    """
    text = error if isinstance(error, str) else str(error)
    masked = redact_pii(redact_text(text))
    return masked if isinstance(masked, str) else text


# Log fields holding text the Runtime does not choose: an exception's message and traceback
# (structlog's format_exc_info and StackInfoRenderer), which can quote a tool's or a provider's
# data.
_UNTRUSTED_LOG_KEYS = ("exception", "stack")


def redact_log_event(logger: Any, method_name: str, event_dict: Any) -> Any:
    """A structlog processor: credentials masked in every field (``redact``) and, in an
    exception's traceback (``exception``, ``stack``), personal identifiers too (since 0.2.0b5).

    ``algen_agent_runtime.observability.setup.configure_logging`` installs it. A host with its
    own structlog configuration adds it after ``format_exc_info`` (and before its renderer) to
    mask its own log lines the same way; the Runtime's loggers then pass their events through
    unchanged, exception objects included (see docs/operations.md#redacted-telemetry).
    """
    redacted = redact(dict(event_dict))
    for key in _UNTRUSTED_LOG_KEYS:
        value = redacted.get(key)
        if isinstance(value, str):
            redacted[key] = redact_error_text(value)
    return redacted


def hidden_value_marker(value: Any) -> str:
    """What an approver sees in place of a credential: ``[REDACTED len=<n> sha256=<8 hex>]``.

    ``n`` is the length of the value (a non-string value is measured as its JSON) and the hash
    is the first 8 hex digits of its SHA-256, so an approver can tell a 40-character key from a
    4 KB payload, and compare a value with one they know, without seeing it.
    """
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    )
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"[REDACTED len={len(text)} sha256={digest}]"


def _redact_parameter(value: Any) -> Any:
    if isinstance(value, Enum):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            key: hidden_value_marker(item) if is_sensitive_key(key) else _redact_parameter(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [_redact_parameter(item) for item in value]
    return value


def redact_parameters(value: Mapping[str, Any]) -> dict[str, Any]:
    """Tool arguments as an approval request shows them.

    The value of a key that names a credential (``is_sensitive_key``, at any depth) becomes
    ``hidden_value_marker(value)``, ``[REDACTED len=<n> sha256=<8 hex>]``, and credentials
    inside any string value are masked as ``redact_text`` masks them.

    The runtime stores an approval's ``redacted_parameters`` through this, and the approval
    routes return ``modified_parameters`` through it, so both read the same way. An agent
    run refuses to ask for approval of a call whose hidden keys its tool's input schema does
    not declare (``undeclared_sensitive_parameters``), since the approver could not judge them.
    """
    result = _redact_parameter(dict(value))
    assert isinstance(result, dict)
    return result


def undeclared_sensitive_parameters(arguments: Mapping[str, Any], schema: Any) -> list[str]:
    """The dotted paths of keys in ``arguments`` that ``redact_parameters`` hides and that
    ``schema`` (a tool's JSON input schema) does not declare under ``properties``.

    An approval request shows a declared credential as a length and hash; one the schema does
    not declare is something the model added, which the approver could not see, so the Runtime
    refuses the call instead of asking.
    """
    from algen_agent_runtime.policies.engine import _SchemaNode

    found: list[str] = []

    def walk(value: Any, node: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                child = node.property(key) if node is not None else None
                where = f"{path}.{key}" if path else str(key)
                if is_sensitive_key(key):
                    if child is None:
                        found.append(where)
                    continue
                walk(item, child, where)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            child = node.items() if node is not None else None
            for index, item in enumerate(value):
                walk(item, child, f"{path}[{index}]")

    walk(arguments, _SchemaNode.root(schema), "")
    return found


# The kinds of identifier redact_pii finds, as guardrail_policy.pii.exempt_entities names them.
PII_ENTITIES = ("email", "phone", "aadhaar", "card")


def redact_pii(
    value: Any,
    *,
    entities: Iterable[str] | None = None,
    exempt: Iterable[str] = (),
    schema: Mapping[str, Any] | None = None,
) -> Any:
    """``value`` with personal identifiers masked as the ``pii`` guardrail masks them.

    ``value`` is a string, or a dict, list or tuple of them (nested at any depth; a Pydantic
    model works too); other values are returned as they are, and the input is never modified.
    The rules are the guardrail's own (see docs/agent-tooling.md#what-the-pii-guardrail-redacts):
    ``[REDACTED_EMAIL]``, ``[REDACTED_PHONE]`` (a ten-digit Indian mobile number written as one,
    or with a word such as "phone" before it or in its field's name, such as ``mobile_no``),
    ``[REDACTED_AADHAAR]`` (Verhoeff-checked) and ``[REDACTED_CARD]`` (network prefix and Luhn).

    ``entities`` limits the kinds redacted (default: all of ``PII_ENTITIES``); ``exempt`` leaves
    kinds alone, as ``guardrail_policy.pii.exempt_entities`` does. With ``schema``, the JSON
    Schema ``value`` follows (a tool's input or output schema), fields declaring
    ``"format": "identifier"`` or ``"x-pii": false`` keep a single-token value and fields
    declaring ``"format": "email"`` keep a value that is exactly one address.

    Raises ``ValueError`` for an unknown kind.
    """
    from algen_agent_runtime.policies.engine import PIIRedactionPolicy, _SchemaNode

    selected = set(PII_ENTITIES if entities is None else entities)
    skipped = set(exempt)
    unknown = (selected | skipped) - set(PII_ENTITIES)
    if unknown:
        raise ValueError(
            f"unknown PII kind(s) {sorted(unknown)}; expected any of {list(PII_ENTITIES)}"
        )
    off = frozenset(kind for kind in PII_ENTITIES if kind not in selected or kind in skipped)
    node = _SchemaNode.root(schema) if schema is not None else None
    return PIIRedactionPolicy()._redact(value, node, None, off)[0]


def redact_secrets(value: Any) -> Any:
    """``value`` with credentials masked as ``[REDACTED]``, as the ``secrets`` guardrail does.

    ``value`` is a string, or a dict, list or tuple of them at any depth; the input is never
    modified. In text, credentials are masked by ``redact_secret_text``'s rules: the value of a
    credential written as code or configuration (``api_key=...``, ``"password": "..."``,
    ``SECRET: ...``), bearer and other ``Authorization`` credentials, URL passwords
    (``postgresql://user:pw@host``), PEM private keys and tokens recognisable by shape
    (``sk-...``, ``AKIA...``, ``ghp_...``, ``xoxb-...``, JWTs); "The secret: consistency" is
    left alone. In a dict, the whole value of a key that names a credential (``password``,
    ``api_key``, ``db.password``, ``githubToken``, ``access_token``, ``client_secret``,
    ``x-api-key``, ``authorization``, ``cookie``; not ``max_tokens``) is replaced, whatever it
    holds.
    """

    def walk(item: Any) -> Any:
        if isinstance(item, Enum):
            return item
        if isinstance(item, str):
            return redact_text(item)
        if isinstance(item, Mapping):
            return {
                key: REDACTED if is_sensitive_key(key) else walk(child)
                for key, child in item.items()
            }
        if isinstance(item, (list, tuple)):
            items = [walk(child) for child in item]
            return tuple(items) if isinstance(item, tuple) else items
        return item

    return walk(value)
