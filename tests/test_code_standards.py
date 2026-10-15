"""Enforce the project's function-size standard: at most 15 code lines per function.

Counted lines are the function body's non-blank, non-comment lines, excluding the
signature, decorators and the docstring. Long SQL or literal tables belong in module-level
constants, not inside functions.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAX_CODE_LINES = 15
SOURCES = sorted([*(ROOT / "src").rglob("*.py"), ROOT / "app.py"])


def _has_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    first = node.body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def _code_lines(lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    body = node.body[1:] if _has_docstring(node) else node.body
    if not body:
        return 0
    span = lines[body[0].lineno - 1 : node.end_lineno]
    return sum(1 for line in span if line.strip() and not line.strip().startswith("#"))


def _functions() -> list[tuple[str, int]]:
    found = []
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                where = f"{path.relative_to(ROOT)}:{node.lineno}:{node.name}"
                found.append((where, _code_lines(lines, node)))
    return found


@pytest.mark.parametrize(("where", "n_lines"), _functions(), ids=str)
def test_function_has_at_most_15_code_lines(where: str, n_lines: int) -> None:
    assert n_lines <= MAX_CODE_LINES, f"{where} has {n_lines} code lines (max {MAX_CODE_LINES})"
