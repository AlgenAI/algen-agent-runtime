"""A conservative structural check of SQL that a model generated outside the compiler.

This is not a SQL parser. It tokenizes one statement (string literals, quoted identifiers,
numbers, parameters, punctuation), refuses anything it cannot read unambiguously, and answers
narrow questions about the top-level ``SELECT``: whether a governed table is read exactly once,
and whether its top-level ``WHERE`` clause is an AND-only conjunction that contains a given
``column <op> <constant>`` predicate. Every doubt is a refusal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from algen_agent_runtime.exceptions.errors import ConfigurationError

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_OPERATOR_CHARS = "+-*/<>=!|&^~%@"
_COMPARISONS = frozenset({"=", "<>", "!=", "<", "<=", ">", ">="})
# Words that end the top-level WHERE clause.
_WHERE_END = frozenset(
    {
        "group",
        "order",
        "limit",
        "having",
        "window",
        "offset",
        "fetch",
        "for",
        "union",
        "intersect",
        "except",
        "returning",
    }
)
_SET_OPERATIONS = frozenset({"union", "intersect", "except"})
# Words that may follow a table reference and are not its alias.
_NOT_ALIAS = frozenset(
    {
        "where",
        "join",
        "inner",
        "left",
        "right",
        "full",
        "cross",
        "natural",
        "on",
        "using",
        "lateral",
        "tablesample",
        *_WHERE_END,
    }
)
# Constant words a predicate's right-hand side may contain besides literals and parameters.
_CONSTANT_WORDS = frozenset(
    {
        "true",
        "false",
        "null",
        "current_date",
        "current_timestamp",
        "current_time",
        "localtimestamp",
        "localtime",
        "now",
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "interval",
        "with",
        "zone",
        "cast",
        "as",
        "date_trunc",
    }
)


@dataclass(frozen=True)
class Token:
    kind: str  # ident, string, number, param, op, punct
    text: str
    depth: int


def _refuse(reason: str) -> ConfigurationError:
    return ConfigurationError(f"generated SQL was refused: {reason}")


def tokenize(sql: str) -> list[Token]:
    """Tokens of one statement; raises ``ConfigurationError`` for anything ambiguous."""
    tokens: list[Token] = []
    depth = 0
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char.isspace():
            index += 1
            continue
        if sql.startswith("--", index) or sql.startswith("/*", index) or char == "#":
            raise _refuse("comments are not allowed")
        if char == "'":
            end = index + 1
            while True:
                end = sql.find("'", end)
                if end == -1:
                    raise _refuse("unterminated string literal")
                if end + 1 < length and sql[end + 1] == "'":
                    end += 2
                    continue
                break
            text = sql[index + 1 : end].replace("''", "'")
            if "\\" in text:
                # E'' strings (and standard_conforming_strings=off) end at a different quote.
                raise _refuse("backslashes in string literals are not allowed")
            tokens.append(Token("string", text, depth))
            index = end + 1
            continue
        if char == '"':
            end = index + 1
            while True:
                end = sql.find('"', end)
                if end == -1:
                    raise _refuse("unterminated quoted identifier")
                if end + 1 < length and sql[end + 1] == '"':
                    end += 2
                    continue
                break
            tokens.append(Token("ident", sql[index + 1 : end].replace('""', '"').lower(), depth))
            index = end + 1
            continue
        if char == "$":
            match = re.compile(r"\$\d+").match(sql, index)
            if match is None:
                raise _refuse("dollar-quoted strings are not allowed")
            tokens.append(Token("param", match.group(), depth))
            index = match.end()
            continue
        if char == "?":
            tokens.append(Token("param", "?", depth))
            index += 1
            continue
        if char == ":":
            if sql.startswith("::", index):
                tokens.append(Token("op", "::", depth))
                index += 2
                continue
            match = _IDENTIFIER.match(sql, index + 1)
            if match is None:
                raise _refuse("unexpected ':'")
            tokens.append(Token("param", ":" + match.group(), depth))
            index = match.end()
            continue
        number = _NUMBER.match(sql, index)
        if number is not None and (char.isdigit() or char == "."):
            if char == "." and tokens and tokens[-1].kind == "ident":
                tokens.append(Token("punct", ".", depth))
                index += 1
                continue
            tokens.append(Token("number", number.group(), depth))
            index = number.end()
            continue
        identifier = _IDENTIFIER.match(sql, index)
        if identifier is not None:
            tokens.append(Token("ident", identifier.group().lower(), depth))
            index = identifier.end()
            continue
        if char == "(":
            tokens.append(Token("punct", "(", depth))
            depth += 1
            index += 1
            continue
        if char == ")":
            depth -= 1
            if depth < 0:
                raise _refuse("unbalanced parentheses")
            tokens.append(Token("punct", ")", depth))
            index += 1
            continue
        if char in ",.;[]":
            tokens.append(Token("punct", char, depth))
            index += 1
            continue
        if char in _OPERATOR_CHARS:
            end = index
            while end < length and sql[end] in _OPERATOR_CHARS:
                end += 1
            operator = sql[index:end]
            if "--" in operator or "/*" in operator:
                raise _refuse("comments are not allowed")
            tokens.append(Token("op", operator, depth))
            index = end
            continue
        raise _refuse(f"unexpected character {char!r}")
    if depth != 0:
        raise _refuse("unbalanced parentheses")
    while tokens and tokens[-1].text == ";" and tokens[-1].kind == "punct":
        tokens.pop()
    if any(token.kind == "punct" and token.text == ";" for token in tokens):
        raise _refuse("only one statement is allowed")
    if not tokens:
        raise _refuse("the statement is empty")
    if tokens[0].kind != "ident" or tokens[0].text not in {"select", "with"}:
        raise _refuse("it must be one SELECT statement")
    return tokens


def _chains(tokens: list[Token]) -> list[tuple[int, int, list[str]]]:
    """Dotted identifier chains as (start, end-exclusive, parts)."""
    chains: list[tuple[int, int, list[str]]] = []
    index = 0
    while index < len(tokens):
        if tokens[index].kind != "ident" or (
            index > 0 and tokens[index - 1].kind == "punct" and tokens[index - 1].text == "."
        ):
            index += 1
            continue
        parts = [tokens[index].text]
        end = index + 1
        while (
            end + 1 < len(tokens)
            and tokens[end].text == "."
            and tokens[end].kind == "punct"
            and tokens[end + 1].kind == "ident"
        ):
            parts.append(tokens[end + 1].text)
            end += 2
        chains.append((index, end, parts))
        index = end
    return chains


@dataclass(frozen=True)
class TableReference:
    qualifiers: frozenset[str]


def table_reference(tokens: list[Token], table: str) -> TableReference:
    """The single top-level reference to ``table``; refuses none, several or a nested one."""
    parts = table.lower().split(".")
    matches = [
        (start, end, chain)
        for start, end, chain in _chains(tokens)
        if chain[-len(parts) :] == parts
        and (end >= len(tokens) or tokens[end].text != "(")
        and len(chain) <= len(parts) + 1
    ]
    if not matches:
        raise _refuse(f"it must read {table!r}")
    if len(matches) > 1:
        raise _refuse(f"it must read {table!r} exactly once")
    start, end, chain = matches[0]
    if tokens[start].depth != 0:
        raise _refuse(f"{table!r} must be read by the top-level query, not a subquery")
    if any(t.kind == "ident" and t.depth == 0 and t.text in _SET_OPERATIONS for t in tokens):
        raise _refuse("set operations (UNION, INTERSECT, EXCEPT) are not allowed")
    qualifiers = {".".join(chain), chain[-1], ".".join(parts)}
    position = end
    if position < len(tokens) and tokens[position].kind == "ident":
        if tokens[position].text == "as":
            position += 1
        if (
            position < len(tokens)
            and tokens[position].kind == "ident"
            and tokens[position].text not in _NOT_ALIAS
        ):
            qualifiers = {tokens[position].text}
    return TableReference(frozenset(qualifiers))


def where_conjuncts(tokens: list[Token]) -> list[list[Token]]:
    """The AND-separated parts of the top-level WHERE clause; refuses OR at its top level."""
    starts = [
        index
        for index, token in enumerate(tokens)
        if token.kind == "ident" and token.depth == 0 and token.text == "where"
    ]
    if not starts:
        raise _refuse("it must have a top-level WHERE clause")
    if len(starts) > 1:
        raise _refuse("it has more than one top-level WHERE clause")
    clause: list[Token] = []
    for token in tokens[starts[0] + 1 :]:
        if token.depth == 0 and token.kind == "ident" and token.text in _WHERE_END:
            break
        clause.append(token)
    base = 0
    while (
        len(clause) >= 2
        and clause[0].text == "("
        and clause[-1].text == ")"
        and clause[0].depth == base
        and not any(t.depth == base for t in clause[1:-1])
    ):
        clause = clause[1:-1]
        base += 1
    conjuncts: list[list[Token]] = [[]]
    in_between = False
    for token in clause:
        top = token.depth == base and token.kind == "ident"
        if top and token.text == "or":
            raise _refuse("OR is not allowed at the top level of the WHERE clause")
        if top and token.text == "between":
            in_between = True
        elif top and token.text == "and":
            if in_between:
                in_between = False
            else:
                conjuncts.append([])
                continue
        conjuncts[-1].append(token)
    if any(not conjunct for conjunct in conjuncts):
        raise _refuse("the WHERE clause is malformed")
    return conjuncts


def _column(conjunct: list[Token], column: str, qualifiers: frozenset[str]) -> int | None:
    """The index after a reference to ``column`` that starts ``conjunct``."""
    chain: list[str] = []
    index = 0
    while index < len(conjunct) and conjunct[index].kind == "ident":
        chain.append(conjunct[index].text)
        index += 1
        if (
            index < len(conjunct)
            and conjunct[index].text == "."
            and conjunct[index].kind == "punct"
        ):
            index += 1
            continue
        break
    if not chain or chain[-1] != column:
        return None
    if len(chain) > 1 and ".".join(chain[:-1]) not in qualifiers:
        return None
    return index


def _constant(tokens: list[Token]) -> bool:
    """Literals, parameters, arithmetic, casts and constant words only: no columns."""
    if not tokens:
        return False
    for position, token in enumerate(tokens):
        if token.kind in {"string", "number", "param"}:
            continue
        if token.kind == "op" and token.text in {"+", "-", "*", "/", "::"}:
            continue
        if token.kind == "punct" and token.text in {"(", ")", ","}:
            continue
        if token.kind == "ident":
            if token.text in _CONSTANT_WORDS:
                continue
            if position > 0 and tokens[position - 1].text in {"::", "as"}:
                continue  # a cast's type name
        return False
    return True


def has_constant_predicate(
    conjuncts: list[list[Token]], column: str, qualifiers: frozenset[str]
) -> bool:
    """Whether a conjunct is ``column <comparison|IN|BETWEEN> <constant>``."""
    for conjunct in conjuncts:
        index = _column(conjunct, column, qualifiers)
        if index is None or index >= len(conjunct):
            continue
        operator = conjunct[index]
        rest = conjunct[index + 1 :]
        if operator.kind == "op" and operator.text in _COMPARISONS:
            if _constant(rest):
                return True
        elif operator.kind == "ident" and operator.text == "in":
            if rest and rest[0].text == "(" and rest[-1].text == ")" and _constant(rest):
                return True
        elif operator.kind == "ident" and operator.text == "between":
            ands = [
                i
                for i, t in enumerate(rest)
                if t.kind == "ident" and t.text == "and" and t.depth == operator.depth
            ]
            if len(ands) == 1 and _constant(rest[: ands[0]]) and _constant(rest[ands[0] + 1 :]):
                return True
    return False


def has_boolean_predicate(
    conjuncts: list[list[Token]], column: str, qualifiers: frozenset[str], value: bool
) -> bool:
    """Whether a conjunct is ``column = <value>`` or ``column IS <value>``."""
    literal = "true" if value else "false"
    for conjunct in conjuncts:
        index = _column(conjunct, column, qualifiers)
        if index is None or len(conjunct) != index + 2:
            continue
        operator, rhs = conjunct[index], conjunct[index + 1]
        if (
            operator.text in {"=", "is"}
            and operator.kind in {"op", "ident"}
            and rhs.kind == "ident"
            and rhs.text == literal
        ):
            return True
    return False
