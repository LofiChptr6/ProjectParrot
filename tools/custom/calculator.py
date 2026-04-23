"""Custom tool: calculate — safe math expression evaluator."""

import ast
import math
import operator

TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "Evaluate a math expression safely. Supports arithmetic (+, -, *, /, **), "
            "comparisons, and math functions (sqrt, sin, cos, log, abs, round, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Math expression to evaluate (e.g. 'sqrt(144) + 3 * 2')",
                },
            },
            "required": ["expression"],
        },
    },
}

# Allowed math functions
_SAFE_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "ceil": math.ceil,
    "floor": math.floor,
    "pow": math.pow,
    "pi": math.pi,
    "e": math.e,
}

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node):
    """Recursively evaluate an AST node with only safe operations."""
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant: {node.value!r}")
    elif isinstance(node, ast.BinOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    elif isinstance(node, ast.UnaryOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only simple function calls allowed")
        fname = node.func.id
        func = _SAFE_FUNCS.get(fname)
        if func is None:
            raise ValueError(f"Unknown function: {fname}")
        if callable(func):
            args = [_safe_eval(a) for a in node.args]
            return func(*args)
        raise ValueError(f"{fname} is a constant, not callable")
    elif isinstance(node, ast.Name):
        val = _SAFE_FUNCS.get(node.id)
        if val is not None and not callable(val):
            return val  # constants like pi, e
        raise ValueError(f"Unknown variable: {node.id}")
    else:
        raise ValueError(f"Unsupported expression: {type(node).__name__}")


async def execute(arguments: dict) -> str:
    """Evaluate a math expression safely."""
    expr = arguments.get("expression", "")
    if not expr:
        return "Error: no expression provided."

    try:
        tree = ast.parse(expr, mode="eval")
        result = _safe_eval(tree)
        # Format nicely
        if isinstance(result, float) and result == int(result) and abs(result) < 1e15:
            return str(int(result))
        return str(round(result, 10) if isinstance(result, float) else result)
    except Exception as e:
        return f"Error: {e}"
