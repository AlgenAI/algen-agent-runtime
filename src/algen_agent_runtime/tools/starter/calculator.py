from __future__ import annotations

import ast
import math
from typing import Any

from algen_agent_runtime.tools.contracts import (
    Idempotency,
    SideEffect,
    Tool,
    ToolContext,
    ToolDefinition,
)

MAX_EXPRESSION_LENGTH = 500
MAX_AST_NODES = 100
MAX_AST_DEPTH = 15
MAX_EXPONENT = 1000
MAX_RESULT_MAGNITUDE = 1e300

SAFE_FUNCTIONS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
}


def _check_ast_safety(node: ast.AST, depth: int = 0) -> int:
    if depth > MAX_AST_DEPTH:
        raise ValueError(f"expression exceeds maximum nesting depth of {MAX_AST_DEPTH}")

    total_nodes = 1
    match node:
        case ast.Expression(body=body):
            total_nodes += _check_ast_safety(body, depth + 1)
        case ast.BinOp(left=left, op=op, right=right):
            if not isinstance(
                op,
                (
                    ast.Add,
                    ast.Sub,
                    ast.Mult,
                    ast.Div,
                    ast.FloorDiv,
                    ast.Mod,
                    ast.Pow,
                ),
            ):
                raise ValueError(f"disallowed binary operator: {type(op).__name__}")
            total_nodes += _check_ast_safety(left, depth + 1)
            total_nodes += _check_ast_safety(right, depth + 1)
        case ast.UnaryOp(op=op, operand=operand):
            if not isinstance(op, (ast.UAdd, ast.USub)):
                raise ValueError(f"disallowed unary operator: {type(op).__name__}")
            total_nodes += _check_ast_safety(operand, depth + 1)
        case ast.Constant(value=val):
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ValueError(f"constants must be numeric, got: {type(val).__name__}")
        case ast.Call(func=func, args=args, keywords=[]):
            if not isinstance(func, ast.Name):
                raise ValueError("disallowed function call syntax")
            fn_name = func.id
            if fn_name not in SAFE_FUNCTIONS:
                raise ValueError(f"unsupported or disallowed function call: {fn_name!r}")
            if len(args) > 10:
                raise ValueError(f"too many arguments to function {fn_name!r}")
            for arg in args:
                total_nodes += _check_ast_safety(arg, depth + 1)
        case _:
            raise ValueError(f"disallowed syntax: {type(node).__name__}")

    return total_nodes


def _eval_node(node: ast.AST) -> float | int:
    match node:
        case ast.Expression(body=body):
            return _eval_node(body)
        case ast.Constant(value=val):
            assert isinstance(val, (int, float))
            return val
        case ast.UnaryOp(op=ast.UAdd(), operand=operand):
            return +_eval_node(operand)
        case ast.UnaryOp(op=ast.USub(), operand=operand):
            return -_eval_node(operand)
        case ast.BinOp(left=left, op=op, right=right):
            lval = _eval_node(left)
            rval = _eval_node(right)
            match op:
                case ast.Add():
                    res = lval + rval
                case ast.Sub():
                    res = lval - rval
                case ast.Mult():
                    res = lval * rval
                case ast.Div():
                    if rval == 0:
                        raise ValueError("division by zero")
                    res = lval / rval
                case ast.FloorDiv():
                    if rval == 0:
                        raise ValueError("division by zero")
                    res = lval // rval
                case ast.Mod():
                    if rval == 0:
                        raise ValueError("modulo by zero")
                    res = lval % rval
                case ast.Pow():
                    if abs(rval) > MAX_EXPONENT:
                        raise ValueError(f"exponent {rval} exceeds limit of {MAX_EXPONENT}")
                    if abs(lval) > 1 and rval > 1000:
                        raise ValueError("result would exceed maximum computational bounds")
                    try:
                        res = lval**rval
                    except OverflowError as exc:
                        raise ValueError("arithmetic overflow") from exc
                case _:
                    raise ValueError(f"unsupported operator: {type(op).__name__}")

            if isinstance(res, (int, float)) and math.isinf(res):
                raise ValueError("arithmetic overflow")
            if isinstance(res, (int, float)) and math.isnan(res):
                raise ValueError("arithmetic undefined (NaN)")
            if abs(res) > MAX_RESULT_MAGNITUDE:
                raise ValueError("result exceeds maximum allowable magnitude")
            return res
        case ast.Call(func=ast.Name(id=fn_name), args=args):
            fn = SAFE_FUNCTIONS[fn_name]
            evaluated_args = [_eval_node(arg) for arg in args]
            try:
                res = fn(*evaluated_args)  # type: ignore[operator]
            except (ValueError, ZeroDivisionError) as exc:
                raise ValueError(f"function {fn_name} failed: {exc}") from exc
            if not isinstance(res, (int, float)):
                raise ValueError(f"function {fn_name} returned non-numeric result")
            if math.isinf(res) or math.isnan(res):
                raise ValueError(f"function {fn_name} returned non-finite result")
            return res
        case _:
            raise ValueError(f"cannot evaluate node: {type(node).__name__}")


def evaluate_expression(expression: str) -> float | int:
    """Safely evaluate a mathematical expression using an AST allowlist."""
    clean_expr = expression.strip()
    if not clean_expr:
        raise ValueError("empty expression")
    if len(clean_expr) > MAX_EXPRESSION_LENGTH:
        raise ValueError(f"expression exceeds maximum length of {MAX_EXPRESSION_LENGTH} characters")

    try:
        tree = ast.parse(clean_expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression syntax: {exc.msg}") from exc

    node_count = _check_ast_safety(tree)
    if node_count > MAX_AST_NODES:
        raise ValueError(f"expression exceeds maximum node limit of {MAX_AST_NODES}")

    return _eval_node(tree)


def calculator_tool() -> Tool:
    """Create a safe starter calculator tool."""

    async def execute(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        expr = arguments["expression"]
        result = evaluate_expression(str(expr))
        return {"result": result}

    return Tool(
        ToolDefinition(
            name="starter.calculator",
            version="1.0.0",
            description="Evaluate a bounded mathematical expression without code execution.",
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Mathematical expression (e.g. '2 + 2 * 3', 'sqrt(16)', 'max(10, 20)')",
                    },
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "result": {"type": "number"},
                },
                "required": ["result"],
            },
            side_effect=SideEffect.NONE,
            idempotency=Idempotency.IDEMPOTENT,
        ),
        execute,
    )
