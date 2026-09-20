from __future__ import annotations

import operator
from collections.abc import Callable
from typing import Any

from algen_agent_runtime.analytics.contracts import (
    AnalyticalNode,
    AnalyticalResult,
    GraphExecutionContext,
    ResultProvenance,
)
from algen_agent_runtime.exceptions.errors import ConfigurationError


def builtin_handlers() -> dict[str, Callable[..., Any]]:
    return {
        "join_results": join_results,
        "compare_periods": compare_periods,
        "calculate": calculate,
        "rank": rank_results,
        "verify": verify_results,
        "compose": compose_results,
    }


def _result(node: AnalyticalNode, value: Any) -> AnalyticalResult:
    return AnalyticalResult(
        node_id=node.id,
        value=value,
        row_count=len(value) if isinstance(value, list) else None,
        provenance=ResultProvenance(
            upstream_result_ids=tuple(reference.node_id for reference in node.inputs.values())
        ),
    )


async def join_results(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    left = inputs.get("left")
    right = inputs.get("right")
    keys = tuple(node.configuration.get("keys", ()))
    relationship = node.configuration.get("relationship", "one_to_one")
    if not isinstance(left, list) or not isinstance(right, list) or not keys:
        raise ConfigurationError("join_results requires left/right row lists and join keys")
    index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for row in right:
        key = tuple(row.get(item) for item in keys)
        index.setdefault(key, []).append(row)
    if relationship in {"one_to_one", "many_to_one"} and any(
        len(rows) > 1 for rows in index.values()
    ):
        raise ConfigurationError("join would fan out beyond its declared relationship")
    output = []
    for row in left:
        matches = index.get(tuple(row.get(item) for item in keys), [])
        if not matches:
            output.append(dict(row))
        else:
            for match in matches:
                output.append(
                    {**row, **{key: value for key, value in match.items() if key not in keys}}
                )
    return _result(node, output)


async def compare_periods(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    current = inputs.get("current")
    baseline = inputs.get("baseline")
    metric = str(node.configuration.get("metric", "value"))
    keys = tuple(node.configuration.get("keys", ()))
    if not isinstance(current, list) or not isinstance(baseline, list):
        raise ConfigurationError("compare_periods requires current and baseline row lists")
    baseline_index = {tuple(row.get(key) for key in keys): row for row in baseline}
    rows = []
    for row in current:
        prior = baseline_index.get(tuple(row.get(key) for key in keys), {})
        current_value = row.get(metric)
        baseline_value = prior.get(metric)
        delta = (
            float(current_value) - float(baseline_value)
            if isinstance(current_value, (int, float)) and isinstance(baseline_value, (int, float))
            else None
        )
        rows.append(
            {
                **row,
                f"baseline_{metric}": baseline_value,
                f"delta_{metric}": delta,
                f"percent_change_{metric}": (
                    delta / float(baseline_value)
                    if delta is not None and float(baseline_value) != 0
                    else None
                ),
            }
        )
    return _result(node, rows)


async def calculate(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    rows = inputs.get("rows")
    operation_name = str(node.configuration.get("operation"))
    left = str(node.configuration.get("left"))
    right = node.configuration.get("right")
    output = str(node.configuration.get("output", "calculated_value"))
    operations: dict[str, Callable[[float, float], float | None]] = {
        "add": operator.add,
        "subtract": operator.sub,
        "multiply": operator.mul,
        "divide": lambda a, b: a / b if b else None,
        "percent_change": lambda a, b: (a - b) / b if b else None,
    }
    if not isinstance(rows, list) or operation_name not in operations:
        raise ConfigurationError("calculate requires rows and an approved arithmetic operation")
    function = operations[operation_name]
    result = []
    for row in rows:
        a = row.get(left)
        b = row.get(right) if isinstance(right, str) else right
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            value = None
        else:
            value = function(float(a), float(b))
        result.append({**row, output: value})
    return _result(node, result)


async def rank_results(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    rows = inputs.get("rows")
    field = str(node.configuration.get("field"))
    descending = bool(node.configuration.get("descending", True))
    limit = int(node.configuration.get("limit", 10))
    if not isinstance(rows, list):
        raise ConfigurationError("rank requires a rows input")
    ranked = sorted(
        rows,
        key=lambda row: (row.get(field) is not None, row.get(field)),
        reverse=descending,
    )[:limit]
    return _result(node, [{**row, "rank": index} for index, row in enumerate(ranked, 1)])


async def verify_results(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    required = tuple(node.configuration.get("required_inputs", inputs.keys()))
    missing = tuple(name for name in required if name not in inputs or inputs[name] is None)
    return _result(node, {"valid": not missing, "missing_inputs": missing})


async def compose_results(
    node: AnalyticalNode, inputs: dict[str, Any], context: GraphExecutionContext
) -> AnalyticalResult:
    del context
    return _result(node, {"sections": inputs, "format": node.configuration.get("format", "json")})
