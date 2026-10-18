from __future__ import annotations

import json

from algen_agent_runtime.context.packing import estimate_message, pack_messages
from algen_agent_runtime.exceptions.errors import BudgetExceededError, ConfigurationError
from algen_agent_runtime.types.contracts import ModelProfile, ModelRequest, TextBlock


def estimate_request_overhead(request: ModelRequest) -> int:
    """Estimate non-message input, including tool definitions and response schemas."""
    payload = {
        "tools": [tool.model_dump(mode="json") for tool in request.tools],
        "response_schema": request.response_schema,
        "extensions": request.extensions,
    }
    return (len(json.dumps(payload, ensure_ascii=False)) + 3) // 4


def estimate_request_tokens(request: ModelRequest) -> int:
    return estimate_request_overhead(request) + sum(
        estimate_message(message) for message in request.messages
    )


def prepare_model_request(request: ModelRequest, profile: ModelProfile) -> ModelRequest:
    """Create a bounded candidate view without modifying the original request.

    Limits are deployment-supplied; this deliberately does not guess model capacities.
    Estimates cover normalized text requests, not provider-specific wire tokenization.
    """
    candidate = request.model_copy(
        update={"model": profile.model, "extensions": profile.extensions}
    )
    if profile.context_window_tokens is None:
        return candidate
    if candidate.max_output_tokens is None or candidate.max_output_tokens < 1:
        raise ConfigurationError(
            "a configured context window requires an explicit positive max_output_tokens"
        )
    # Compatible adapters allow extensions to override wire fields. Such overrides
    # must not bypass the limits established by this normalized request.
    reserved = {
        "model",
        "messages",
        "input",
        "prompt",
        "system",
        "tools",
        "functions",
        "response_format",
        "max_tokens",
        "max_completion_tokens",
        "max_output_tokens",
        "max_new_tokens",
    }
    if reserved.intersection(candidate.extensions):
        raise ConfigurationError(
            "context budgeting does not allow extensions to override model, input, tools, "
            "response format, or output limits; use normalized request fields"
        )
    if any(
        not isinstance(block, TextBlock)
        for message in candidate.messages
        for block in message.content
    ):
        raise ConfigurationError(
            "context budgeting currently supports text content only; image/file token costs "
            "require provider-specific estimation"
        )
    available = (
        profile.context_window_tokens
        - candidate.max_output_tokens
        - profile.context_safety_margin_tokens
        - estimate_request_overhead(candidate)
    )
    if available < 1:
        raise BudgetExceededError(
            "model context window has no room for messages after output, schema, tool, "
            "and safety reservations"
        )
    return candidate.model_copy(update={"messages": pack_messages(candidate.messages, available)})
