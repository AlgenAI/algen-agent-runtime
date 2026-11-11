from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from enum import Enum
from typing import Any, Protocol

import structlog
from opentelemetry import metrics, trace
from pydantic import BaseModel

from algen_agent_runtime.events.contracts import AuditEvent
from algen_agent_runtime.exceptions.errors import PolicyDeniedError
from algen_agent_runtime.policies.contracts import (
    PAYLOAD_CHANGE_KEY,
    POLICY_ERROR_REASON_CODE,
    PolicyAction,
    PolicyDecision,
    PolicyErrorHandling,
    PolicyFailMode,
    PolicyPoint,
)
from algen_agent_runtime.tools.contracts import SideEffect
from algen_agent_runtime.types.contracts import Message, Role, TextBlock

_CONTENT_BOUNDARIES = frozenset(PolicyPoint) - {
    PolicyPoint.AFTER_MEMORY_WRITE,
    PolicyPoint.ATTACHMENTS,
}


class Policy(Protocol):
    name: str
    category: str
    enforcement_mode: str
    applies_to: frozenset[PolicyPoint]

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision: ...


# A key that names a credential: the whole key or its last part (``secret``, ``SECRET_KEY``,
# ``db.password``, ``githubToken``); ``max_tokens`` or ``token_count`` are not.
_SECRET_KEY = (
    r"[A-Za-z0-9_.-]*?"
    r"(?:api[_-]?key|access[_-]?key|secret(?:[_-]?key)?|password|passwd|private[_-]?key|token)"
)

# Keys that are also ordinary words: after one of these, ``key: value`` may be prose.
_PROSE_SECRET_KEYS = frozenset({"secret", "password", "passwd", "token"})


def _looks_like_credential(value: str) -> bool:
    """A value written as prose after ``secret:`` or ``token:`` is redacted only when it reads
    as a credential: it has a digit, letters in mixed case inside it, or it is long and varied.
    ``The secret: consistency`` is left alone."""
    if any(character.isdigit() for character in value):
        return True
    if len(value) >= 12 and re.search(r"[a-z][A-Z]|[A-Z]{2}[a-z]", value):
        return True
    return len(value) >= 16 and len(set(value)) >= 10


class SecretRedactionPolicy:
    """Redact credentials written as ``key: value`` or ``key=value``, and bearer tokens.

    A value is redacted when its key is written the way code and configuration write it (an
    identifier such as ``api_key`` or ``SECRET``, a quoted key, or ``=``), when the value is
    quoted, or when the value itself reads as a credential (see ``_looks_like_credential``).
    Prose such as "The secret: consistency" is left alone.
    """

    name = "secrets"
    category = "input_validation"
    enforcement_mode = "warn"
    applies_to = _CONTENT_BOUNDARIES
    _pattern = re.compile(
        r"(?i)(?P<kq>[\"']?)(?<![A-Za-z0-9_.-])(?P<key>" + _SECRET_KEY + r")(?![A-Za-z0-9_])"
        r"(?P=kq)\s*(?P<op>[:=])\s*(?P<vq>[\"']?)(?P<value>[A-Za-z0-9_\-/.]{8,})"
    )
    # ``Authorization: Bearer <token>``: a token has a digit or a separator, so prose such as
    # "bearer instruments" is left alone.
    _bearer = re.compile(
        r"(?i)\bbearer\s+((?=[A-Za-z0-9._~+/-]*[0-9._~+/-])[A-Za-z0-9._~+/-]{8,}=*)"
    )

    @staticmethod
    def _is_secret(match: re.Match[str]) -> bool:
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

    @classmethod
    def _redact_text(cls, text: str) -> tuple[str, int]:
        count = 0

        def keyed(match: re.Match[str]) -> str:
            nonlocal count
            if not cls._is_secret(match):
                return match.group(0)
            count += 1
            start, end = match.span("value")
            offset = match.start()
            whole = match.group(0)
            return whole[: start - offset] + "[REDACTED]" + whole[end - offset :]

        transformed = cls._pattern.sub(keyed, text)
        transformed, found = cls._bearer.subn(
            lambda match: match.group(0).replace(match.group(1), "[REDACTED]"), transformed
        )
        return transformed, count + found

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        transformed, changed = self._redact(payload)
        if changed:
            # A redaction, not a rewrite: at the tool-argument boundaries it refuses the call
            # rather than sending the tool a masked credential.
            return PolicyDecision(
                action=PolicyAction.REDACT,
                reason_code="secret.redacted",
                reason="Potential secret was redacted.",
                value=transformed,
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="secret.clear", reason="No secret detected."
        )

    def _redact(self, value: Any) -> tuple[Any, bool]:
        if isinstance(value, Enum):
            # StrEnum members are str instances; rewriting them would drop the enum type.
            return value, False
        if isinstance(value, str):
            transformed, count = self._redact_text(value)
            return transformed, bool(count)
        if isinstance(value, BaseModel):
            updates: dict[str, Any] = {}
            changed = False
            for name in type(value).model_fields:
                item, item_changed = self._redact(getattr(value, name))
                updates[name] = item
                changed = changed or item_changed
            return value.model_copy(update=updates) if changed else value, changed
        if isinstance(value, Mapping):
            transformed_mapping: dict[Any, Any] = {}
            changed = False
            for key, item in value.items():
                transformed_item, item_changed = self._redact(item)
                transformed_mapping[key] = transformed_item
                changed = changed or item_changed
            return transformed_mapping if changed else value, changed
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            transformed_items = []
            changed = False
            for item in value:
                transformed_item, item_changed = self._redact(item)
                transformed_items.append(transformed_item)
                changed = changed or item_changed
            if not changed:
                return value, False
            return tuple(transformed_items) if isinstance(value, tuple) else transformed_items, True
        return value, False


_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
# 13 to 19 digits, optionally grouped by spaces or hyphens; redacted only when the Luhn check
# passes, so order numbers and timestamps are left alone.
_CARD = re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])")


# Issuer prefixes and lengths of the major card networks (Visa, Mastercard, American Express,
# Discover, JCB, Diners Club, UnionPay). Without them, one in ten 13-19 digit identifiers would
# pass the Luhn check: millisecond timestamps, order and tracking numbers.
_CARD_BRANDS = (
    (re.compile(r"4"), frozenset({13, 16, 19})),
    (re.compile(r"5[1-5]|2(?:2[2-9]|[3-6]\d|7[01]|720)"), frozenset({16})),
    (re.compile(r"3[47]"), frozenset({15})),
    (re.compile(r"6(?:011|5|4[4-9])"), frozenset({16, 19})),
    (re.compile(r"35(?:2[89]|[3-8]\d)"), frozenset({16, 19})),
    (re.compile(r"3(?:0[0-5]|[689])"), frozenset({14, 16, 19})),
    (re.compile(r"62"), frozenset({16, 17, 18, 19})),
)


def _is_card(number: str) -> bool:
    """A grouped or plain digit run that has a card network's prefix and length and passes
    the Luhn check."""
    digits = "".join(character for character in number if character.isdigit())
    return _luhn(digits) and any(
        prefix.match(digits) and len(digits) in lengths for prefix, lengths in _CARD_BRANDS
    )


def _luhn(number: str) -> bool:
    digits = [int(character) for character in number if character.isdigit()]
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# Verhoeff's dihedral-group tables: the last digit of an Aadhaar number is its check digit.
_VERHOEFF_MULTIPLY = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_PERMUTE = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff(number: str) -> bool:
    """Whether a digit string ends in its Verhoeff check digit, as every Aadhaar number does.
    Nine in ten other 12-digit identifiers (invoice, order and account numbers) fail."""
    check = 0
    digits = [int(character) for character in number if character.isdigit()]
    for index, digit in enumerate(reversed(digits)):
        check = _VERHOEFF_MULTIPLY[check][_VERHOEFF_PERMUTE[index % 8][digit]]
    return check == 0


# Identifiers must stand alone: a digit run inside a UUID, hash or other token (a letter, digit,
# underscore or hyphen next to it) is not a phone or Aadhaar number, nor are three groups of a
# longer grouped number (``2345 6789 0124 5678``) or the digits of a decimal.
_AADHAAR = re.compile(
    r"(?<![\w-])(?<!\d{4}[\s-])(?<!\d\.)"
    r"[2-9]\d{3}(?P<separator>[\s-]?)\d{4}(?P=separator)\d{4}"
    r"(?![\w-])(?![\s-]\d{4}(?!\d))(?!\.\d)"
)
# An Indian mobile number: ten digits starting 6 to 9, perhaps with +91, 91 or 0 before them.
_PHONE = re.compile(
    r"(?<![\w+-])(?<!\d\.)"
    r"(?P<prefix>\+91[\s.-]?|91[\s.-]?|0)?"
    r"[6-9]\d{4}(?P<separator>[\s.-]?)\d{5}"
    r"(?![\w-])(?!\.\d)"
)
# Words that say a bare ten-digit number near them is a phone number. Without one (or +91, a
# leading 0, or 5-5 grouping), a ten-digit run is an order, ticket or chat id, and is left alone.
_PHONE_WORDS = frozenset(
    {
        "call",
        "called",
        "calling",
        "calls",
        "cell",
        "cellphone",
        "contact",
        "dial",
        "mob",
        "mobile",
        "ph",
        "phone",
        "phones",
        "sms",
        "tel",
        "telephone",
        "whats",
        "whatsapp",
    }
)
# Words in prose and in identifiers: ``contactPhone`` and ``mobile_no`` give contact, phone and
# mobile, no.
_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
_PHONE_CONTEXT_CHARS = 40
# A value with no whitespace: what an identifier field may hold and still be exempt.
_SINGLE_TOKEN = re.compile(r"\S{1,512}")


def _mentions_phone(text: str) -> bool:
    return any(word.lower() in _PHONE_WORDS for word in _WORDS.findall(text))


def _exempt_entities(context: Mapping[str, Any]) -> frozenset[str]:
    """The identifier kinds the agent's ``guardrail_policy.pii.exempt_entities`` keeps."""
    guardrails = getattr(context.get("agent"), "guardrail_policy", None)
    exempt = getattr(getattr(guardrails, "pii", None), "exempt_entities", None)
    return frozenset(exempt or ())


_TOOL_ARGUMENT_POINTS = frozenset({PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL})
# The output schema of a tool, by name (None when unknown).
_ToolSchemas = Callable[[str], Any]


def without_policy_hints(schema: Any) -> Any:
    """``schema`` without the hints only the policy engine reads (``"x-pii"`` and
    ``"format": "identifier"``), for sending to a model provider. Some providers (Gemini's
    function declarations) refuse keywords and formats they don't know."""
    if isinstance(schema, Mapping):
        return {
            key: without_policy_hints(value)
            for key, value in schema.items()
            if key != "x-pii" and not (key == "format" and value == "identifier")
        }
    if isinstance(schema, list):
        return [without_policy_hints(item) for item in schema]
    return schema


class _SchemaNode:
    """A position in a tool's JSON Schema, following ``properties``, ``items``, local
    ``$ref`` and ``anyOf``/``oneOf``/``allOf`` alternatives."""

    def __init__(self, nodes: tuple[Mapping[str, Any], ...], root: Mapping[str, Any]) -> None:
        self._nodes = nodes
        self._root = root

    @classmethod
    def root(cls, schema: Any) -> _SchemaNode | None:
        if not isinstance(schema, Mapping):
            return None
        return cls((schema,), schema)._expand()

    def _resolve(self, node: Mapping[str, Any], depth: int = 0) -> list[Mapping[str, Any]]:
        if depth > 16:
            return []
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/"):
            target: Any = self._root
            for part in reference[2:].split("/"):
                target = target.get(part) if isinstance(target, Mapping) else None
            if isinstance(target, Mapping):
                return self._resolve(target, depth + 1)
            return []
        resolved = [node]
        for keyword in ("anyOf", "oneOf", "allOf"):
            for branch in node.get(keyword) or ():
                if isinstance(branch, Mapping):
                    resolved.extend(self._resolve(branch, depth + 1))
        return resolved

    def _expand(self) -> _SchemaNode | None:
        nodes = tuple(item for node in self._nodes for item in self._resolve(node))
        return _SchemaNode(nodes, self._root) if nodes else None

    def _children(self, keyword: str, name: str | None = None) -> _SchemaNode | None:
        found: list[Mapping[str, Any]] = []
        for node in self._nodes:
            child = node.get(keyword)
            if name is not None:
                child = child.get(name) if isinstance(child, Mapping) else None
            if isinstance(child, Mapping):
                found.append(child)
        return _SchemaNode(tuple(found), self._root)._expand() if found else None

    def property(self, name: Any) -> _SchemaNode | None:
        return self._children("properties", str(name))

    def items(self) -> _SchemaNode | None:
        return self._children("items")

    def is_email(self) -> bool:
        return any(node.get("format") == "email" for node in self._nodes)

    def is_identifier(self) -> bool:
        """``"format": "identifier"`` or ``"x-pii": false``: the field holds an id (a chat,
        message or order id), never a person's details. Both are JSON Schema annotations that
        validators, model providers and earlier Runtimes ignore."""
        return any(
            node.get("format") == "identifier" or node.get("x-pii") is False for node in self._nodes
        )


class PIIRedactionPolicy:
    """Redact common direct identifiers before they cross an agent boundary.

    The patterns are deliberately conservative. Deployments can register a specialised
    DLP policy for jurisdiction- or institution-specific identifiers.

    - An email address is redacted wherever it appears.
    - A card number is redacted when it has a card network's prefix and length and passes the
      Luhn check.
    - An Aadhaar number is redacted when it is a whole 12-digit token (perhaps grouped 4-4-4)
      whose last digit is its Verhoeff check digit.
    - A ten-digit Indian mobile number is redacted when it is written as one: with ``+91``,
      ``91`` or ``0`` before it, grouped 5-5 (``98765 43210``), or with a word such as "phone",
      "mobile" or "call" up to 40 characters before it or in its field's name. A bare ten-digit
      run is otherwise an order, ticket or chat id, and is left alone.

    A tool argument whose input schema declares ``"format": "email"`` and whose value is
    exactly one email address is the tool's declared purpose (a recipient, say), so it is
    left as is; every other argument, such as a subject or body, is still redacted. The same
    holds for a tool result field whose output schema declares it (a sender's address), so an
    agent can reply to the mail it just read; a message body is still redacted.

    A tool field whose schema declares ``"format": "identifier"`` or ``"x-pii": false`` (a chat,
    message or order id) is left as is when its value is a single token with no whitespace,
    whatever it looks like. An agent's ``guardrail_policy.pii.exempt_entities`` turns kinds of
    identifier (``email``, ``phone``, ``aadhaar``, ``card``) off for that agent everywhere.
    """

    name = "pii"
    category = "pii"
    enforcement_mode = "warn"
    applies_to = _CONTENT_BOUNDARIES

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        schema: Any = None
        tool_schemas: _ToolSchemas | None = None
        if point in _TOOL_ARGUMENT_POINTS:
            schema = getattr(context.get("tool"), "input_schema", None)
        elif point == PolicyPoint.AFTER_TOOL:
            schema = getattr(context.get("tool"), "output_schema", None)
        elif point in {PolicyPoint.AFTER_RETRIEVAL, PolicyPoint.BEFORE_MODEL}:
            # The tool results in the history keep the fields their tool declares as addresses.
            tool_schemas = context.get("tool_output_schema")
        transformed, labels = self._redact(
            payload, _SchemaNode.root(schema), tool_schemas, _exempt_entities(context)
        )
        if labels:
            return PolicyDecision(
                action=PolicyAction.REDACT,
                reason_code="pii.redacted",
                reason="Direct personal identifiers were redacted.",
                value=transformed,
                audit_metadata={"categories": sorted(labels), "count": len(labels)},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="pii.clear", reason="No PII detected."
        )

    def redact_value(self, value: Any) -> Any:
        """``value`` with the identifiers this policy finds replaced, at any depth."""
        return self._redact(value)[0]

    @staticmethod
    def _redact_text(text: str, exempt: frozenset[str], field: str) -> tuple[str, set[str]]:
        labels: set[str] = set()
        transformed = text
        if "card" not in exempt:
            transformed = _CARD.sub(
                lambda match: "[REDACTED_CARD]" if _is_card(match.group(0)) else match.group(0),
                transformed,
            )
            if transformed != text:
                labels.add("card")
        if "email" not in exempt:
            transformed, count = _EMAIL.subn("[REDACTED_EMAIL]", transformed)
            if count:
                labels.add("email")
        if "phone" not in exempt:
            named = _mentions_phone(field)
            current = transformed

            def phone(match: re.Match[str]) -> str:
                prefix = match.group("prefix") or ""
                window = current[max(0, match.start() - _PHONE_CONTEXT_CHARS) : match.start()]
                if (
                    # +91 or a trunk 0, or 91 set apart ("91 98765..."), not a bare 91.
                    prefix.startswith(("+", "0"))
                    or prefix.endswith((" ", "-", "\t"))
                    # Grouped 5-5 ("98765 43210"); a dot could be a decimal.
                    or match.group("separator") in (" ", "-", "\t")
                    or named
                    or _mentions_phone(window)
                ):
                    labels.add("phone")
                    return "[REDACTED_PHONE]"
                return match.group(0)

            transformed = _PHONE.sub(phone, current)
        if "aadhaar" not in exempt:

            def aadhaar(match: re.Match[str]) -> str:
                if _verhoeff(match.group(0)):
                    labels.add("aadhaar")
                    return "[REDACTED_AADHAAR]"
                return match.group(0)

            transformed = _AADHAAR.sub(aadhaar, transformed)
        return transformed, labels

    def _redact(
        self,
        value: Any,
        schema: _SchemaNode | None = None,
        tool_schemas: _ToolSchemas | None = None,
        exempt: frozenset[str] = frozenset(),
        field: str = "",
    ) -> tuple[Any, set[str]]:
        if isinstance(value, Enum):
            return value, set()
        if isinstance(value, str):
            if schema is not None and (
                (schema.is_email() and _EMAIL.fullmatch(value))
                or (schema.is_identifier() and _SINGLE_TOKEN.fullmatch(value))
            ):
                return value, set()
            return self._redact_text(value, exempt, field)
        if (
            tool_schemas is not None
            and isinstance(value, Message)
            and value.role is Role.TOOL
            and value.name
        ):
            output = _SchemaNode.root(tool_schemas(value.name))
            if output is not None:
                return self._redact_tool_message(value, output, exempt)
        if isinstance(value, BaseModel):
            updates: dict[str, Any] = {}
            model_labels: set[str] = set()
            for name in type(value).model_fields:
                item, item_labels = self._redact(
                    getattr(value, name),
                    schema.property(name) if schema else None,
                    tool_schemas,
                    exempt,
                    name,
                )
                updates[name] = item
                model_labels.update(item_labels)
            return value.model_copy(update=updates) if model_labels else value, model_labels
        if isinstance(value, Mapping):
            transformed_mapping: dict[Any, Any] = {}
            mapping_labels: set[str] = set()
            for key, item in value.items():
                transformed_item, item_labels = self._redact(
                    item,
                    schema.property(key) if schema else None,
                    tool_schemas,
                    exempt,
                    key if isinstance(key, str) else "",
                )
                transformed_mapping[key] = transformed_item
                mapping_labels.update(item_labels)
            return transformed_mapping if mapping_labels else value, mapping_labels
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            transformed_items = []
            sequence_labels: set[str] = set()
            for item in value:
                # A list keeps its field's name: ``phones: [...]`` holds phone numbers.
                transformed_item, item_labels = self._redact(
                    item, schema.items() if schema else None, tool_schemas, exempt, field
                )
                transformed_items.append(transformed_item)
                sequence_labels.update(item_labels)
            if not sequence_labels:
                return value, sequence_labels
            transformed_sequence = (
                tuple(transformed_items) if isinstance(value, tuple) else transformed_items
            )
            return transformed_sequence, sequence_labels
        return value, set()

    def _redact_tool_message(
        self, message: Message, output: _SchemaNode, exempt: frozenset[str] = frozenset()
    ) -> tuple[Message, set[str]]:
        """A tool result the model is about to read, its JSON redacted against the tool's
        output schema, so declared address fields stay as they were after ``after_tool``."""
        blocks: list[Any] = []
        labels: set[str] = set()
        for block in message.content:
            if isinstance(block, TextBlock):
                try:
                    decoded = json.loads(block.text)
                except ValueError:
                    decoded = None
                else:
                    redacted, found = self._redact(decoded, output, exempt=exempt)
                    if found:
                        block = TextBlock(text=json.dumps(redacted, default=str))
                    labels.update(found)
                    blocks.append(block)
                    continue
            redacted_block, found = self._redact(block, exempt=exempt)
            blocks.append(redacted_block)
            labels.update(found)
        if not labels:
            return message, labels
        return message.model_copy(update={"content": tuple(blocks)}), labels


class AuthorizationPolicy:
    name = "authorization"
    category = "tool_permission"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.BEFORE_TOOL})

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if point == "before_tool":
            definition = context["tool"]
            tool_context = context["context"]
            if not definition.required_permissions.issubset(tool_context.permissions):
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    reason_code="auth.missing_permission",
                    reason="Tool permission check failed.",
                )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="auth.allowed", reason="Authorized."
        )


class SideEffectPolicy:
    name = "side_effects"
    category = "tool_permission"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.PLAN_TOOL})

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        definition = context.get("tool")
        approval_policy = context.get("approval_policy")
        if point == "plan_tool" and definition and approval_policy:
            side_effect = definition.side_effect
            required = (
                definition.requires_approval
                or (
                    approval_policy.require_for_destructive
                    and side_effect == SideEffect.DESTRUCTIVE
                )
            ) or (
                approval_policy.require_for_side_effects
                and side_effect in {SideEffect.WRITE, SideEffect.EXTERNAL, SideEffect.DESTRUCTIVE}
            )
            if required:
                return PolicyDecision(
                    action=PolicyAction.REQUIRE_APPROVAL,
                    reason_code="side_effect.approval_required",
                    reason=f"Tool {definition.name} has {side_effect.value} side effects.",
                    audit_metadata={"risk": side_effect.value},
                )
        return PolicyDecision(
            action=PolicyAction.ALLOW, reason_code="side_effect.allowed", reason="Allowed."
        )


# Where an instruction to the assistant starts: the start of the text or of a clause, or after
# words that address it ("please", "and", "you must", "I want you to", "can you"). A question
# about overrides ("how do we detect users who try to bypass our safety policy?") has none of
# these before its verb, so it passes.
_ADDRESSED = (
    r"(?:^|(?<=[.!?;:,\n\"'()\[\]{}<>*#|-])"
    r"|\b(?:please|now|just|and|then|also|so|instead|first|simply)\s+"
    r"|\byou\s+(?:must|should|will|shall|need\s+to|have\s+to|are\s+to|can|may)\s+"
    r"|\b(?:can|could|will|would)\s+you\s+(?:please\s+)?"
    r"|\b(?:want|need|order|command|instruct|tell|ask)\s+you\s+to\s+)\s*"
)
# What an override aims at: the assistant's own rules. A business rule ("override the leave
# policy") is not one; "your policy", the safety policy and the system prompt are.
_GUARDS = (
    r"(?:guardrails?|safeguards?"
    r"|(?:safety|content|moderation|usage|ethical)\s+(?:polic(?:y|ies)|filters?|rules|guidelines"
    r"|guardrails?|restrictions|safeguards?)"
    r"|(?:system|developer)\s+(?:prompt|message|instructions?))"
)
_OWN_RULES = (
    r"your\s+(?:own\s+|current\s+|previous\s+|prior\s+|original\s+|built-?in\s+)?"
    r"(?:rules|restrictions|filters?|instructions?|guidelines|programming|polic(?:y|ies)|limits"
    r"|limitations|training|directives|constraints|guardrails?|safeguards?)"
)
_OVERRIDE = r"(?:bypass|circumvent|disable|override|deactivate|turn\s+off|switch\s+off|ignore)"

INSTRUCTION_OVERRIDE_PATTERNS = (
    re.compile(
        r"(?i)\bignore\s+(?:all\s+(?:of\s+)?)?(?:your\s+|the\s+)?"
        r"(?:previous|prior|above|system|developer)\s+instructions?\b"
    ),
    re.compile(
        r"(?i)" + _ADDRESSED + r"(?:reveal|print|show|repeat|expose|output|display|leak)\b"
        r".{0,40}\b(?:system|developer)\s+(?:prompt|message|instructions?)\b"
    ),
    re.compile(
        r"(?i)" + _ADDRESSED + _OVERRIDE + r"\s+(?:(?:all|any|every)\s+(?:of\s+)?)?"
        r"(?:(?:the|these|those|its|your)\s+)?(?:own\s+|current\s+|built-?in\s+|existing\s+)?"
        + _GUARDS
        + r"\b"
    ),
    re.compile(
        r"(?i)" + _ADDRESSED + r"(?:" + _OVERRIDE + r"|forget|disregard)\s+"
        r"(?:(?:all|any)\s+(?:of\s+)?)?" + _OWN_RULES + r"\b"
    ),
    re.compile(
        r"(?i)\b(?:do anything now|DAN mode|jailbreak mode|developer mode enabled)\b"
        r"|\b(?:you\s+are|you're|act\s+as|enter|enable|activate|switch\s+to)\s+(?:now\s+)?"
        r"(?:in\s+)?(?:an?\s+)?(?:jailbroken|jailbreak|DAN|unrestricted\s+mode)\b"
    ),
)


def looks_like_instruction_override(text: str) -> bool:
    """High-signal attempts to override or disclose privileged instructions."""
    return any(pattern.search(text) for pattern in INSTRUCTION_OVERRIDE_PATTERNS)


class PromptInjectionPolicy:
    """Block clear attempts to override or disclose privileged instructions.

    This intentionally targets high-signal imperative attacks. Applications can
    replace it with a specialised classifier without changing orchestration.
    """

    name = "prompt_injection"
    category = "prompt_injection"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.INPUT})
    _patterns = INSTRUCTION_OVERRIDE_PATTERNS

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        del context
        if point != "input" or not isinstance(payload, str):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="prompt_injection.not_applicable",
                reason="Prompt-injection scan is not applicable at this boundary.",
            )
        if any(pattern.search(payload) for pattern in self._patterns):
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason_code="prompt_injection.detected",
                reason="The request contains an attempt to override privileged instructions.",
                audit_metadata={"risk": "prompt_injection", "intervention": "blocked"},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="prompt_injection.clear",
            reason="No high-confidence prompt-injection pattern detected.",
        )


def _strings(value: Any) -> Iterator[str]:
    """The string values in a parsed JSON value, at any depth (keys are not values)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


class OutputValidationPolicy:
    """Apply deterministic safety checks before model output reaches a user."""

    name = "output_validation"
    category = "output_validation"
    enforcement_mode = "block"
    applies_to = frozenset({PolicyPoint.AFTER_MODEL, PolicyPoint.FINAL_RESPONSE})
    _privileged_marker = re.compile(
        r"(?i)(?:^|\n)\s*(?:system|developer)\s+(?:prompt|message|instructions?)\s*:"
    )

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        if point not in {"after_model", "final_response"}:
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="output_validation.not_applicable",
                reason="Output validation is not applicable at this boundary.",
            )
        if context.get("structured_output") and isinstance(payload, (Mapping, list)):
            # Structured output (a request with a response_schema): the parsed JSON answer.
            # Its string values are checked one by one, as the text a reader will see.
            if any(self._privileged_marker.search(text) for text in _strings(payload)):
                return PolicyDecision(
                    action=PolicyAction.DENY,
                    reason_code="output_validation.privileged_instructions",
                    reason="The response appears to disclose privileged instructions.",
                    audit_metadata={"risk": "instruction_disclosure", "intervention": "blocked"},
                )
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="output_validation.passed",
                reason="The response passed deterministic output checks.",
            )
        if (not isinstance(payload, str) or not payload.strip()) and context.get("tool_calls"):
            return PolicyDecision(
                action=PolicyAction.ALLOW,
                reason_code="output_validation.tool_calls",
                reason="An empty assistant message is valid while requesting tools.",
            )
        if not isinstance(payload, str) or not payload.strip():
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason_code="output_validation.empty",
                reason="The generated response was empty.",
            )
        if self._privileged_marker.search(payload):
            return PolicyDecision(
                action=PolicyAction.DENY,
                reason_code="output_validation.privileged_instructions",
                reason="The response appears to disclose privileged instructions.",
                audit_metadata={"risk": "instruction_disclosure", "intervention": "blocked"},
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="output_validation.passed",
            reason="The response passed deterministic output checks.",
        )


# The boundaries that see a tool's arguments or result: an agent's
# ``guardrail_policy.tool_policies`` selects the policies that run there.
_TOOL_POINTS = frozenset({PolicyPoint.PLAN_TOOL, PolicyPoint.BEFORE_TOOL, PolicyPoint.AFTER_TOOL})
POLICY_ERROR_AUDIT_ACTION = "policy.error"
POLICY_ERROR_METRIC_NAME = "algen_agent_runtime.policy.errors"
_logger = structlog.get_logger("algen_agent_runtime.policies")


def _error_counter() -> Any:
    try:
        return metrics.get_meter("algen_agent_runtime.policies").create_counter(
            POLICY_ERROR_METRIC_NAME,
            unit="1",
            description="Policy evaluations that raised or timed out, by boundary and fail mode",
        )
    except Exception:
        return None


class _AuditLog(Protocol):
    async def append(self, event: AuditEvent) -> None: ...


class CompositePolicyEngine:
    def __init__(
        self,
        policies: Sequence[Policy] | None = None,
        guardrail_scope: Callable[..., AbstractContextManager[Any]] | None = None,
        *,
        error_handling: PolicyErrorHandling | None = None,
        audits: _AuditLog | None = None,
    ) -> None:
        configured = policies or (
            SecretRedactionPolicy(),
            PIIRedactionPolicy(),
            PromptInjectionPolicy(),
            OutputValidationPolicy(),
            AuthorizationPolicy(),
            SideEffectPolicy(),
        )
        self._policies: dict[str, Policy] = {policy.name: policy for policy in configured}
        # Authorization and side-effect approval are runtime security invariants; policies from
        # a policy bundle join them, so every agent runs them whatever its guardrail selection.
        self._mandatory: set[str] = {"authorization", "side_effects"}
        self.error_handling = error_handling or PolicyErrorHandling()
        self._audits = audits
        self._error_counter = _error_counter()
        self._tracer = trace.get_tracer("algen_agent_runtime.guardrails")
        self._guardrail_scope = guardrail_scope or self._otel_guardrail_scope

    def _otel_guardrail_scope(
        self,
        *,
        name: str,
        category: str,
        enforcement_mode: str,
        policy_id: str | None = None,
    ) -> AbstractContextManager[Any]:
        attributes: dict[str, Any] = {
            "span.type": "guardrail",
            "guardrail.name": name,
            "guardrail.category": category,
            "guardrail.enforcement_mode": enforcement_mode,
            "guardrail.source_sdk": "algen_agent_runtime",
            "guardrail.evidence_type": "span_attribute",
        }
        if policy_id:
            attributes["guardrail.policy_id"] = policy_id
        return self._tracer.start_as_current_span(f"guardrail.{name}", attributes=attributes)

    def register(self, policy: Policy, *, replace: bool = False, mandatory: bool = False) -> None:
        """Add a policy. A ``mandatory`` one runs for every agent, whatever its selection."""
        if policy.name in self._policies and not replace:
            raise ValueError(f"policy {policy.name!r} is already registered")
        self._policies[policy.name] = policy
        if mandatory:
            self._mandatory.add(policy.name)

    def mandatory(self) -> tuple[str, ...]:
        return tuple(sorted(self._mandatory))

    async def _record_error(self, failure: dict[str, Any], context: Mapping[str, Any]) -> None:
        """Count, log and audit a policy that raised or timed out. Never the error's text: it
        may quote the payload."""
        attributes = {
            "boundary": failure["boundary"],
            "policy": failure["policy"],
            "fail_mode": failure["fail_mode"],
            "error_type": failure["error_type"],
        }
        if self._error_counter is not None:
            try:
                self._error_counter.add(1, attributes)
            except Exception:
                pass
        _logger.warning("policy_error", **attributes, timed_out=failure["timed_out"])
        tool_context = context.get("context")
        tenant_id = context.get("tenant_id") or getattr(tool_context, "tenant_id", None)
        if self._audits is None or not tenant_id:
            return
        user_id = context.get("user_id") or getattr(tool_context, "user_id", None)
        run_id = context.get("run_id") or getattr(tool_context, "run_id", None)
        tool = context.get("tool")
        metadata = {**failure}
        if tool is not None and getattr(tool, "name", None):
            metadata["tool"] = tool.name
        try:
            await self._audits.append(
                AuditEvent(
                    action=POLICY_ERROR_AUDIT_ACTION,
                    outcome=f"fail_{failure['fail_mode']}",
                    tenant_id=str(tenant_id),
                    actor_id=str(user_id or "system"),
                    resource_id=str(run_id or failure["boundary"]),
                    metadata=metadata,
                )
            )
        except Exception:
            _logger.exception("policy_error_audit_failed", **attributes)

    def list(self) -> tuple[str, ...]:
        return tuple(self._policies)

    async def evaluate(
        self, point: str, payload: Any, context: Mapping[str, Any]
    ) -> PolicyDecision:
        try:
            policy_point = PolicyPoint(point)
        except ValueError as exc:
            expected = ", ".join(item.value for item in PolicyPoint)
            raise ValueError(
                f"unknown policy point {point!r}; expected one of: {expected}"
            ) from exc
        transformed = payload
        transforms: list[str] = []
        redacted = False
        agent = context.get("agent")
        guardrails = getattr(agent, "guardrail_policy", None)
        selected = set(getattr(guardrails, "policies", ()))
        tool_policies = getattr(guardrails, "tool_policies", None)
        if tool_policies is not None and policy_point in _TOOL_POINTS:
            selected = set(tool_policies)
        selected.update(self._mandatory)
        invoked: list[str] = []
        triggered: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, Any]] = []
        pending: PolicyDecision | None = None
        timeout = self.error_handling.timeout_seconds
        for policy in self._policies.values():
            if agent is not None and policy.name not in selected:
                continue
            applies_to = getattr(policy, "applies_to", None)
            # Policies registered by older plugins remain compatible and apply at
            # every boundary until they declare a narrower applicability contract.
            if applies_to is not None and policy_point not in applies_to:
                skipped.append(policy.name)
                continue
            invoked.append(policy.name)
            with self._guardrail_scope(
                name=policy.name,
                category=policy.category,
                enforcement_mode=getattr(policy, "enforcement_mode", "unknown"),
            ) as guardrail_span:
                guardrail_span.set_attribute("guardrail.boundary", point)
                try:
                    async with asyncio.timeout(timeout):
                        decision = await policy.evaluate(point, transformed, context)
                    if not isinstance(decision, PolicyDecision):
                        raise TypeError("policy returned something other than a PolicyDecision")
                except PolicyDeniedError:
                    # A policy that raises a denial has decided; it is not a policy error.
                    raise
                except Exception as exc:
                    mode = self.error_handling.mode(policy_point)
                    failure = {
                        "boundary": policy_point.value,
                        "policy": policy.name,
                        "error_type": type(exc).__name__,
                        "fail_mode": mode.value,
                        "timed_out": isinstance(exc, TimeoutError),
                    }
                    guardrail_span.set_attribute("guardrail.error", failure["error_type"])
                    guardrail_span.set_attribute("guardrail.fail_mode", mode.value)
                    await self._record_error(failure, context)
                    errors.append(failure)
                    if mode == PolicyFailMode.OPEN:
                        continue
                    return PolicyDecision(
                        action=PolicyAction.DENY,
                        reason_code=POLICY_ERROR_REASON_CODE,
                        reason=(
                            f"Policy {policy.name} could not decide at {policy_point.value}; "
                            "this boundary fails closed."
                        ),
                        audit_metadata={
                            "fail_mode": mode.value,
                            "policy_errors": errors,
                            "policies_invoked": invoked,
                            "policies_triggered": [*triggered, policy.name],
                            "policies_skipped": skipped,
                        },
                    )
                triggered_now = decision.action != PolicyAction.ALLOW
                guardrail_span.set_attribute("guardrail.triggered", triggered_now)
                guardrail_span.set_attribute("guardrail.reason_code", decision.reason_code)
            if decision.action == PolicyAction.DENY:
                return decision.model_copy(
                    update={
                        "audit_metadata": {
                            **decision.audit_metadata,
                            "policies_invoked": invoked,
                            "policies_triggered": [*triggered, policy.name],
                            "policies_skipped": skipped,
                            **self._errors_metadata(errors),
                        }
                    }
                )
            if decision.action in {
                PolicyAction.REQUIRE_CLARIFICATION,
                PolicyAction.REQUIRE_APPROVAL,
            }:
                # The first such answer stands, but the remaining policies still run: a later
                # denial must refuse the call rather than put it to a person first.
                triggered.append(policy.name)
                if pending is None:
                    pending = decision
                continue
            if decision.action in {PolicyAction.REDACT, PolicyAction.TRANSFORM}:
                transformed = decision.value
                transforms.append(decision.reason_code)
                triggered.append(policy.name)
                redacted = redacted or decision.action == PolicyAction.REDACT
        if pending is not None:
            update: dict[str, Any] = {
                "audit_metadata": {
                    **pending.audit_metadata,
                    "policies_invoked": invoked,
                    "policies_triggered": triggered,
                    "policies_skipped": skipped,
                    **self._errors_metadata(errors),
                }
            }
            if transforms:
                # The other policies' changes still stand: plan_tool asks for approval of the
                # rewritten arguments, or refuses a redacted call before anyone is asked.
                update["value"] = transformed
                update["audit_metadata"]["transforms"] = transforms
                update["audit_metadata"][PAYLOAD_CHANGE_KEY] = (
                    PolicyAction.REDACT.value if redacted else PolicyAction.TRANSFORM.value
                )
            return pending.model_copy(update=update)
        if transforms:
            # One redaction makes the combined change a redaction: the boundaries that treat the
            # two differently (the tool-argument ones) must see that something was removed.
            return PolicyDecision(
                action=PolicyAction.REDACT if redacted else PolicyAction.TRANSFORM,
                reason_code="policy.redacted" if redacted else "policy.transformed",
                reason=(
                    "Payload redacted by policy." if redacted else "Payload transformed by policy."
                ),
                value=transformed,
                audit_metadata={
                    "transforms": transforms,
                    "policies_invoked": invoked,
                    "policies_triggered": triggered,
                    "policies_skipped": skipped,
                    **self._errors_metadata(errors),
                },
            )
        return PolicyDecision(
            action=PolicyAction.ALLOW,
            reason_code="policy.allowed",
            reason="Allowed.",
            audit_metadata={
                "policies_invoked": invoked,
                "policies_triggered": triggered,
                "policies_skipped": skipped,
                **self._errors_metadata(errors),
            },
        )

    @staticmethod
    def _errors_metadata(errors: Sequence[dict[str, Any]]) -> dict[str, Any]:
        # Only present when a policy failed open, so ordinary decisions read as they always did.
        if not errors:
            return {}
        return {"fail_mode": PolicyFailMode.OPEN.value, "policy_errors": list(errors)}
