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
    """Return the literal assigned to a module-level name, or None. The last assignment wins, as it would if the module ran."""
    found = None
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
                found = ast.literal_eval(value)
            except (TypeError, ValueError):
                found = None
    return found


def contract_value(brick: Path, name: str) -> Any:
    """Return one literal declared in ``brick/contract.py``, or None if absent or unreadable."""
    path = brick / "contract.py"
    try:
        return assigned_literal(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)), name)
    except (OSError, SyntaxError):
        return None


def brick_ports(brick: Path) -> dict[str, dict[str, Any]]:
    """The declared ports of a brick: port name to its declaration. Not validated.

    Anything that is not a dict of dicts reads as no ports; the linter is the
    place that rejects a malformed ``PORTS``.
    """
    ports = contract_value(brick, "PORTS")
    if not isinstance(ports, dict):
        return {}
    return {name: dict(port) for name, port in ports.items() if isinstance(name, str) and isinstance(port, dict)}


# What the tools compute a brick to be; nothing declares it. In the order they are reviewed.
SHAPES = ("open", "sealed", "entry")


def brick_shape(brick: Path) -> str:
    """``entry`` if the brick has a ``__main__.py``, else ``sealed`` if it declares no ports, else ``open``.

    A contract that is missing or cannot be read as a ports dict reads as ``open``:
    routing must not fail on the very file a change may have broken, and the linter
    is the place that rejects it.
    """
    if (brick / "__main__.py").is_file():
        return "entry"
    ports = contract_value(brick, "PORTS")
    return "sealed" if isinstance(ports, dict) and not ports else "open"
