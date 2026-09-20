from __future__ import annotations

import re

_NAME_TOKEN = re.compile(r"^[A-Za-z][A-Za-z'.-]*$")
_NON_NAME_HEADERS = frozenset(
    {
        "resume",
        "curriculum vitae",
        "summary",
        "profile",
        "professional summary",
        "experience",
    }
)


def candidate_identity_entities(candidate_name: str, resume_text: str) -> tuple[str, ...]:
    """Return explicit identity plus a conservative name-like first-line header."""
    entities = [candidate_name.strip()]
    first_line = next((line.strip() for line in resume_text.splitlines() if line.strip()), "")
    tokens = first_line.split()
    if (
        first_line.casefold() not in _NON_NAME_HEADERS
        and 2 <= len(tokens) <= 5
        and len(first_line) <= 100
        and all(_NAME_TOKEN.fullmatch(token) for token in tokens)
    ):
        entities.append(first_line)
    return tuple(dict.fromkeys(item for item in entities if item))
