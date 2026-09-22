from __future__ import annotations

import zoneinfo
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)


def clock_tool(now_fn: Callable[[], datetime] | None = None) -> Tool:
    """Create a clock and date inspection tool with explicit timezone support.

    ``now_fn`` can be supplied to return a deterministic timestamp for testing.
    """
    is_deterministic = now_fn is not None

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        tz_name = arguments.get("timezone", "UTC")
        try:
            tz = zoneinfo.ZoneInfo(tz_name)
        except zoneinfo.ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {tz_name!r}") from exc

        if now_fn is not None:
            current = now_fn()
            if current.tzinfo is None:
                current = current.replace(tzinfo=UTC)
        else:
            current = datetime.now(UTC)

        localized = current.astimezone(tz)

        return {
            "isoformat": localized.isoformat(),
            "timestamp": localized.timestamp(),
            "year": localized.year,
            "month": localized.month,
            "day": localized.day,
            "hour": localized.hour,
            "minute": localized.minute,
            "second": localized.second,
            "day_of_week": localized.strftime("%A"),
            "timezone": tz_name,
        }

    return Tool(
        ToolDefinition(
            name="starter.clock",
            version="1.0.0",
            description="Inspect the current date, time, and timezone information.",
            input_schema={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "default": "UTC",
                        "description": "IANA timezone name (e.g. 'UTC', 'America/New_York', 'Europe/London', 'Asia/Tokyo').",
                    },
                },
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "isoformat": {"type": "string"},
                    "timestamp": {"type": "number"},
                    "year": {"type": "integer"},
                    "month": {"type": "integer"},
                    "day": {"type": "integer"},
                    "hour": {"type": "integer"},
                    "minute": {"type": "integer"},
                    "second": {"type": "integer"},
                    "day_of_week": {"type": "string"},
                    "timezone": {"type": "string"},
                },
                "required": [
                    "isoformat",
                    "timestamp",
                    "year",
                    "month",
                    "day",
                    "hour",
                    "minute",
                    "second",
                    "day_of_week",
                    "timezone",
                ],
            },
            side_effect=SideEffect.NONE,
            idempotency=Idempotency.IDEMPOTENT if is_deterministic else Idempotency.NON_IDEMPOTENT,
        ),
        execute,
    )
