from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class PresentationAudience(StrEnum):
    BUSINESS = "business"
    DEVELOPER = "developer"


RUN_FAILURE_MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "budget_refused": (
            "Your organization has reached its AI usage limit, so this request was not run. "
            "Please contact your administrator."
        ),
        "limit_exceeded": (
            "This request needed more work than the assistant is allowed for one answer. "
            "Try a narrower question."
        ),
        "timeout": "This request took too long and was stopped. Please try again.",
        "timeout_during_recovery": "This request took too long and was stopped. Please try again.",
    }
)
"""What a user is told when a turn's run fails for one of these reasons (the ``reason`` of the
run's ``run.failed`` event). Fixed, user-safe texts: never the run's error text."""

_GENERIC_FAILURE = "I couldn't complete that request. Please try again."


@dataclass(frozen=True)
class ConversationPresentation:
    """Controls user-facing detail without reducing persisted diagnostics or telemetry."""

    progress_audience: PresentationAudience = PresentationAudience.BUSINESS
    error_audience: PresentationAudience = PresentationAudience.BUSINESS
    show_technical_details: bool = False
    technical_details_expanded: bool = False
    failure_messages: Mapping[str, str] = field(default_factory=lambda: RUN_FAILURE_MESSAGES)
    """User-safe text per run failure reason; pass your own mapping to change or add texts."""

    def run_failure_message(self, reason: str | None, error: str | None) -> str:
        """What the user is told when a turn's run failed with ``reason``.

        A reason in ``failure_messages`` gets its text. Otherwise the business audience gets a
        generic text and the developer audience the run's (already redacted) error.
        """
        if reason and reason in self.failure_messages:
            return self.failure_messages[reason]
        if self.error_audience == PresentationAudience.DEVELOPER and error:
            return error.strip()[:500] or _GENERIC_FAILURE
        return _GENERIC_FAILURE

    def progress(
        self,
        business: dict[str, Any],
        developer: dict[str, Any],
    ) -> dict[str, Any]:
        selected = (
            business if self.progress_audience == PresentationAudience.BUSINESS else developer
        )
        return dict(selected)

    def error_message(
        self,
        error: Exception,
        *,
        business_message: str = (
            "I couldn't complete that request safely. Please try again or refine the question."
        ),
    ) -> str:
        if self.error_audience == PresentationAudience.DEVELOPER:
            return str(error)[:500]
        return business_message

    def unexpected_error_message(self, error: Exception) -> str:
        if self.error_audience == PresentationAudience.DEVELOPER:
            detail = str(error).strip()
            return detail[:500] if detail else type(error).__name__
        return _GENERIC_FAILURE
