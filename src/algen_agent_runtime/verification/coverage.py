from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from algen_agent_runtime.context.packing import REPAIR_MESSAGE_NAME
from algen_agent_runtime.tools.contracts import ResultCoverage
from algen_agent_runtime.types.contracts import Message, Role
from algen_agent_runtime.verification.verifiers import VerificationResult

# Wording that states something does not exist. Deliberately broad: the check runs only when a
# tool in the run reported partial coverage, and a false alarm costs one repair turn.
DEFAULT_ABSENCE_PATTERNS: tuple[str, ...] = (
    r"\bno\b",
    r"\bnone\b",
    r"\bnothing\b",
    r"\bno[- ]one\b",
    r"\bnobody\b",
    r"\bnot found\b",
    r"\bnot (?:on record|recorded|available|present|listed)\b",
    r"\b(?:does|do|did) not (?:exist|have|contain)\b",
    r"\b(?:doesn't|don't|didn't) (?:exist|have|contain)\b",
    r"\b(?:cannot|can't|can not|could not|couldn't) (?:find|locate)\b",
    r"\b(?:was not|wasn't|were not|weren't|is not|isn't|are not|aren't) found\b",
    r"\b(?:unable to|did not|didn't|failed to) (?:find|locate)\b",
    r"\bzero\b",
)

# Wording that tells the user the search may have missed something.
DEFAULT_DISCLOSURE_PATTERNS: tuple[str, ...] = (
    r"\bincomplete\b",
    r"\bpartial\b",
    r"\bnot (?:be )?(?:exhaustive|complete|comprehensive)\b",
    r"\bmay (?:not )?(?:be (?:incomplete|missing|out of date|outdated|stale)|exist)\b",
    r"\bmight (?:not )?(?:be (?:incomplete|missing|out of date|outdated|stale)|exist)\b",
    r"\b(?:cannot|can't|can not|could not|couldn't) (?:confirm|verify|rule out)\b",
    r"\bunable to (?:confirm|verify|rule out)\b",
    r"\bnot (?:confirmed|verified)\b",
    r"\bcapped\b",
    r"\blimited to\b",
    r"\bonly (?:the )?(?:first|top|latest|most recent)\b",
    r"\bsystem of record\b",
    r"\b(?:index|cache|copy|replica) (?:may|might|can)\b",
)

REPAIR_HINT = (
    "The tool results this answer relies on were capped or came from a derived store, so they "
    "do not show that nothing exists. Search the system of record, or keep the answer and say "
    "plainly that the search may be incomplete and what it covered."
)


def run_tool_results(messages: Sequence[Message]) -> list[Message]:
    """The tool results of the current run: those after the user request that started it."""
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role == Role.USER and message.name != REPAIR_MESSAGE_NAME:
            start = index + 1
            break
    return [message for message in messages[start:] if message.role == Role.TOOL]


class ResultCoverageVerifier:
    """Flags an answer that states absence when the run's searches were partial.

    Deterministic, with no model call. It reads the ``coverage`` that tool results report
    (``ResultCoverage``). It fails when the answer matches an absence pattern, some tool result in
    the run was capped or read a derived store, no result for the same ``searched`` collection was
    complete, and the answer does not already disclose that the search may be incomplete. Runs
    whose tools report no coverage pass, so tools without coverage behave as before.
    """

    name = "result_coverage"

    def __init__(
        self,
        absence_patterns: Sequence[str] = DEFAULT_ABSENCE_PATTERNS,
        disclosure_patterns: Sequence[str] = DEFAULT_DISCLOSURE_PATTERNS,
    ) -> None:
        self._absence = [re.compile(pattern, re.IGNORECASE) for pattern in absence_patterns]
        self._disclosure = [re.compile(pattern, re.IGNORECASE) for pattern in disclosure_patterns]

    async def verify(self, value: Any, context: Mapping[str, Any]) -> VerificationResult:
        state = context.get("state")
        history = getattr(state, "context_history", None)
        messages = history if history is not None else getattr(state, "messages", ())
        reported = [
            (message.name or "tool", coverage)
            for message in run_tool_results(messages)
            if (coverage := ResultCoverage.of(message.text_content)) is not None
        ]
        if not reported:
            return _passed("coverage.not_reported", "No tool result reported its coverage.")
        text = str(value or "").replace("\u2019", "'")
        if not any(pattern.search(text) for pattern in self._absence):
            return _passed("coverage.no_absence_claim", "The answer does not state absence.")
        complete = {coverage.searched for _, coverage in reported if coverage.complete}
        partial = [
            (tool, coverage)
            for tool, coverage in reported
            if not coverage.complete and coverage.searched not in complete
        ]
        if not partial:
            return _passed(
                "coverage.complete", "The searches the answer can rest on were complete."
            )
        if any(pattern.search(text) for pattern in self._disclosure):
            return _passed("coverage.disclosed", "The answer says the search may be incomplete.")
        findings = tuple(_finding(tool, coverage) for tool, coverage in partial[:10])
        return VerificationResult(
            passed=False,
            confidence=0,
            reason_code="coverage.partial_absence",
            message="The answer states absence, but the search behind it was partial: "
            + "; ".join(findings),
            repair_hint=REPAIR_HINT,
            findings=findings,
        )


def _passed(reason_code: str, message: str) -> VerificationResult:
    return VerificationResult(passed=True, confidence=1, reason_code=reason_code, message=message)


def _finding(tool: str, coverage: ResultCoverage) -> str:
    # Tool and collection names and counts only: never filter values, which can hold user data.
    parts = []
    if coverage.source == "derived":
        parts.append("read a derived store, not the system of record")
    if coverage.capped or (coverage.total is not None and coverage.returned < coverage.total):
        total = f" of {coverage.total}" if coverage.total is not None else ""
        parts.append(f"capped at {coverage.returned}{total} results")
    return f"{tool} searched {coverage.searched}: {', '.join(parts)}"
