from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_CHART_SPEC_BYTES = 512_000
MAX_INLINE_CHART_ROWS = 1_000
MAX_CHART_DEPTH = 32

_FORBIDDEN_KEYS = frozenset({"url", "href", "expr", "calculate", "usermeta"})
_VIEW_KEYS = frozenset({"mark", "layer", "hconcat", "vconcat", "concat", "facet", "repeat"})


def validate_vega_lite_specification(specification: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a bounded, inline-only Vega-Lite response specification.

    Runtime transports declarative visualizations but never permits a chart to fetch remote data
    or inject expression transforms. Domain adapters remain responsible for choosing useful charts.
    """
    try:
        encoded = json.dumps(specification, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("chart specification must be finite JSON") from exc
    if len(encoded) > MAX_CHART_SPEC_BYTES:
        raise ValueError(f"chart specification exceeds {MAX_CHART_SPEC_BYTES} bytes")
    normalized = json.loads(encoded)
    if not isinstance(normalized, dict) or not normalized:
        raise ValueError("chart specification must be a non-empty object")
    if not _VIEW_KEYS.intersection(normalized):
        raise ValueError("chart specification must define a Vega-Lite view")

    inline_rows = 0

    def inspect(value: Any, depth: int) -> None:
        nonlocal inline_rows
        if depth > MAX_CHART_DEPTH:
            raise ValueError("chart specification nesting is too deep")
        if isinstance(value, dict):
            for key, nested in value.items():
                if str(key).lower() in _FORBIDDEN_KEYS:
                    raise ValueError(f"chart specification field {key!r} is not permitted")
                if key == "values":
                    if not isinstance(nested, list):
                        raise ValueError("chart data.values must be an array")
                    inline_rows += len(nested)
                inspect(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested, depth + 1)

    inspect(normalized, 0)
    if inline_rows > MAX_INLINE_CHART_ROWS:
        raise ValueError(f"chart inline data exceeds {MAX_INLINE_CHART_ROWS} rows")
    return normalized
