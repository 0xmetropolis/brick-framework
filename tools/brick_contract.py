"""Read a brick's contract.py without importing it. Standard library only.

The linter and the graph tool both need this; repo-local checks built on the
framework should import it too instead of copying it or reading contracts with
a regular expression.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def assigned_literal(tree: ast.Module, name: str) -> Any:
    """Return the literal assigned to a module-level name, or None."""
    for node in tree.body:
        value = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value = node.value
        if value is not None:
            try:
                return ast.literal_eval(value)
            except (TypeError, ValueError):
                return None
    return None


def contract_value(brick: Path, name: str) -> Any:
    """Return one literal declared in ``brick/contract.py``, or None if absent or unreadable."""
    path = brick / "contract.py"
    try:
        return assigned_literal(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), name)
    except (OSError, SyntaxError):
        return None
