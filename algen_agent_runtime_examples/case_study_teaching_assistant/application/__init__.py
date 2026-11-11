"""Application-specific policies and conversation adapter for the teaching demo."""

from .handler import TeachingAssistantConversationHandler
from .policies import AcademicIntegrityPolicy, EducationalEquityPolicy
from .tools import register_teaching_tools

__all__ = [
    "AcademicIntegrityPolicy",
    "EducationalEquityPolicy",
    "TeachingAssistantConversationHandler",
    "register_teaching_tools",
]
