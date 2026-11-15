#!/usr/bin/env python3
"""Run one brick from the command line, with its mode: standard library only.

    python3 tools/run_brick.py <brick> [--input FILE|-] [--fresh] [--save] [--test]

This is the way to record or refresh the saved examples of a brick that has no
process door (only a workflow may carry ``__main__.py``): a model brick, a
source adapter's brick. It lives outside the bricks, imports only the brick's
top-level ``run`` and passes the JSON input unchanged. ``--save`` implies fresh,
as for ``run``. Output is JSON on stdout; a failure is one line on stderr and a
nonzero status (``--traceback`` shows the stack).
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any


def encode(value: Any) -> Any:
    """JSON for whatever a brick returns; bytes become a base64 object so nothing is lost."""
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"{type(value).__name__} is not JSON")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("brick", help="folder name under bricks/")
    parser.add_argument("--input", default="-", help="JSON file with the brick input, or - for stdin (default)")
    parser.add_argument("--fresh", action="store_true", help="call the real sources; examples are not changed")
    parser.add_argument("--save", action="store_true", help="call the real sources and replace the saved examples (implies fresh)")
    parser.add_argument("--test", action="store_true", help="write the run record as a local test record (BRICK_RUN_KIND=test)")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--traceback", action="store_true", help="show the stack on failure")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", args.brick) or not (root / "bricks" / args.brick / "contract.py").is_file():
        print(f"error: no brick named {args.brick!r} under {root / 'bricks'}", file=sys.stderr)
        return 2
    try:
        text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
        inputs = json.loads(text)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read the JSON input: {exc}", file=sys.stderr)
        return 2
    if args.test:
        os.environ["BRICK_RUN_KIND"] = "test"
    sys.path.insert(0, str(root))
    try:
        run = importlib.import_module(f"bricks.{args.brick}").run
        output = run(inputs, fresh=args.fresh or args.save, save=args.save)
        print(json.dumps(output, indent=2, sort_keys=True, default=encode))
    except Exception as exc:  # noqa: BLE001
        if args.traceback:
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
