from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PresentationAudience(StrEnum):
    BUSINESS = "business"
    DEVELOPER = "developer"


@dataclass(frozen=True)
class ConversationPresentation:
    """Controls user-facing detail without reducing persisted diagnostics or telemetry."""

    progress_audience: PresentationAudience = PresentationAudience.BUSINESS
    error_audience: PresentationAudience = PresentationAudience.BUSINESS
    show_technical_details: bool = False
    technical_details_expanded: bool = False

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
        return "I couldn't complete that request. Please try again."
