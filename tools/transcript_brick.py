#!/usr/bin/env python3
"""Follow one piece of sample data through a brick project and write the transcript. Standard library only.

    python3 tools/transcript_brick.py <brick> [--root PROJECT] [--input FILE|-] [--name NAME]
                                      [--fresh [PORT ...]] [--record [PORT ...]] [--out DIR]

``<brick>`` is the brick the sample enters, usually an entry brick; ``--input`` is one JSON input
record for it (a file, or stdin). The brick is run once through its own ``run``, with every source
answering from its tape, and each place the value passes through is written down, in order:

* the entry brick, with the sample input;
* each joint crossing into a sibling brick, with what crossed, and the sibling's return;
* each source port touched, with the request, the recorded response, and the take file used;
* the entry brick's output.

Replay is the default. Nothing goes to a live source unless you ask with ``--fresh`` or
``--record`` (port names as for ``run_brick.py``). The ledger entry of the run is local and
gitignored (``BRICK_TEST=1``). Two files are written, with the same hops:

    <out>/<brick>-<name>.md     a Mermaid sequenceDiagram and a hop table
    <out>/<brick>-<name>.json   the hops, for tests and tools

``--out`` defaults to ``<root>/reports/transcripts`` and ``--name`` to the input file's name, so
the path is predictable. Replaying the same project and the same input gives the same bytes: the
report carries no run ID, timestamp or path of this machine. Values are shown in full, with sorted
keys, never truncated. The flow is followed in the order it runs, so a brick that calls a sibling
from several threads is not reported in a fixed order.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from brick_contract import brick_ports, brick_shape  # type: ignore[no-redef]
    from graph_bricks import label_text  # type: ignore[no-redef]
    from mermaid import escape as mermaid_escape  # type: ignore[no-redef]
else:
    from .brick_contract import brick_ports, brick_shape
    from .graph_bricks import label_text
    from .mermaid import escape as mermaid_escape

HOOKED = re.compile(r"bricks\.([a-z][a-z0-9_]*)\.(runner\.run|input\.tape)")
INLINE_LIMIT = 60


def plain(value: Any) -> Any:
    """A value as plain JSON data: tuples are lists, bytes are a base64 object, anything else is its repr."""

    def encode(item: Any) -> Any:
        if isinstance(item, (bytes, bytearray)):
            return {"base64": base64.b64encode(bytes(item)).decode("ascii")}
        return repr(item)

    return json.loads(json.dumps(value, default=encode))


class Tracer:
    """Collects the hops of one run, in order, from hooks on each brick's runner and tape modules."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.hops: list[dict[str, Any]] = []
        self.stack: list[str] = []
        self.suppress = 0
        self._shapes: dict[str, str] = {}

    def shape(self, brick: str) -> str:
        if brick not in self._shapes:
            self._shapes[brick] = brick_shape(self.root / "bricks" / brick)
        return self._shapes[brick]

    def named(self, brick: str) -> str:
        return f"{brick} ({self.shape(brick)})"

    def hop(self, kind: str, where: str, start: str, end: str, how: str, take: str | None, value: Any, request: Any = None) -> None:
        hop: dict[str, Any] = {
            "n": len(self.hops) + 1, "kind": kind, "where": where, "from": start, "to": end,
            "how": how, "take": take, "value": plain(value),
        }
        if request is not None:
            hop["request"] = plain(request)
        self.hops.append(hop)

    def enter(self, brick: str, inputs: Any) -> None:
        if not self.stack:
            self.hop("input", self.named(brick), brick, brick, "sample input", None, inputs)
        else:
            caller = self.stack[-1]
            port = brick_ports(self.root / "bricks" / caller).get(brick, {})
            how = f"joint from {caller}, {port.get('policy', '?')}, pin v{port.get('pin', '?')}"
            if port.get("follow"):
                how += ", follows " + ", ".join(str(item) for item in port["follow"])
            self.hop("joint", f"{self.named(caller)} → {self.named(brick)}", caller, brick, how, None, inputs)
        self.stack.append(brick)

    def leave(self, brick: str, output: Any, *, raised: bool = False) -> None:
        self.stack.pop()
        if not self.stack:
            self.hop("error" if raised else "output", self.named(brick), brick, brick, "raised" if raised else "output", None, output)
        else:
            caller = self.stack[-1]
            self.hop("error" if raised else "return", f"{self.named(brick)} → {self.named(caller)}", brick, caller, "raised" if raised else "returned", None, output)

    def source(self, brick: str, module: Any, source: str, take: str, request: Any, response: Any, how: str, detail: str = "") -> None:
        port = module.PORTS.get(source, {})
        text = f"source epoch {port.get('epoch', '?')}"
        label = label_text(port.get("label"))
        if label:
            text += f" label {label}"
        path = None
        if how in ("replay", "record"):
            path = (Path(module.TAPE_DIR) / source / f"{take}.json").resolve().relative_to(self.root).as_posix()
        elif how == "fresh":
            text += ", fresh (no take used)"
        if how == "record":
            text += ", recorded"
        self.hop("source", f"{self.named(brick)} → {source}", brick, f"{brick}.{source}", text + detail, path, response, request)

    def patch_runner(self, brick: str, module: Any) -> None:
        tracer, real = self, module.execute

        def execute(inputs: Any, context: Any) -> Any:
            tracer.enter(brick, inputs)
            try:
                output = real(inputs, context)
            except BaseException as exc:
                tracer.leave(brick, {"error": {"type": type(exc).__name__, "message": str(exc)}}, raised=True)
                raise
            tracer.leave(brick, output)
            return output

        module.execute = execute

    def patch_tape(self, brick: str, module: Any) -> None:
        tracer = self
        real_once, real_load, real_take, real_call = module.once, module.load, module.Session.take, module.Session.call

        def once(context: Any, source: str, take: str, request: Any, fetch: Any, **options: Any) -> Any:
            tracer.suppress += 1
            try:
                result = real_once(context, source, take, request, fetch, **options)
            except BaseException as exc:
                tracer.suppress -= 1
                tracer.source(brick, module, source, take, request, {"error": {"type": type(exc).__name__, "message": str(exc)}}, module.mode(context, source), ", raised")
                raise
            tracer.suppress -= 1
            tracer.source(brick, module, source, take, request, result[0], module.mode(context, source))
            return result

        def load(source: str, take: str, request: Any) -> Any:
            record = real_load(source, take, request)
            if not tracer.suppress:
                tracer.source(brick, module, source, take, request, record.get("response", record.get("error")), "replay")
            return record

        def session_take(self: Any, window: str, request: Any) -> Any:
            tracer.suppress += 1
            try:
                return real_take(self, window, request)
            finally:
                tracer.suppress -= 1

        def session_call(self: Any, kind: str, request: Any, fetch: Any) -> Any:
            take = f"{self.name}-{self.window}"
            detail = f", session {self.name}, window {self.window}, {kind}"
            try:
                response = real_call(self, kind, request, fetch)
            except BaseException as exc:
                tracer.source(brick, module, self.source, take, request, {"error": {"type": type(exc).__name__, "message": str(exc)}}, self.mode, detail + ", raised")
                raise
            tracer.source(brick, module, self.source, take, request, response, self.mode, detail)
            return response

        module.once, module.load = once, load
        module.Session.take, module.Session.call = session_take, session_call


class _Loader(importlib.abc.Loader):
    """Runs the real loader, then lets the tracer patch the module before anything imports from it."""

    def __init__(self, real: Any, after: Any) -> None:
        self.real, self.after = real, after

    def create_module(self, spec: Any) -> Any:
        return self.real.create_module(spec) if hasattr(self.real, "create_module") else None

    def exec_module(self, module: Any) -> None:
        self.real.exec_module(module)
        self.after(module)


class _Finder(importlib.abc.MetaPathFinder):
    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer
        self.busy: set[str] = set()

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        match = HOOKED.fullmatch(fullname)
        if not match or fullname in self.busy:
            return None
        self.busy.add(fullname)
        try:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        finally:
            self.busy.discard(fullname)
        if spec is None or spec.loader is None:
            return None
        patch = self.tracer.patch_runner if match.group(2) == "runner.run" else self.tracer.patch_tape
        spec.loader = _Loader(spec.loader, lambda module, brick=match.group(1), patch=patch: patch(brick, module))
        return spec


def trace(root: Path, brick: str, inputs: Any, fresh: Any = False, record: Any = False) -> tuple[Tracer, str | None]:
    """Run ``bricks.<brick>.run(inputs)`` once under a tracer. Returns the tracer and the error line, if the run failed."""
    tracer = Tracer(root)
    finder = _Finder(tracer)
    sys.meta_path.insert(0, finder)
    sys.path.insert(0, str(root))
    os.environ["BRICK_TEST"] = "1"
    error = None
    try:
        run = importlib.import_module(f"bricks.{brick}").run
        run(inputs, fresh=fresh, record=record)
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    finally:
        sys.meta_path.remove(finder)
        sys.path.remove(str(root))
    return tracer, error


def html(text: str) -> str:
    """Text safe inside a table cell: HTML-escaped, with the pipe that would end the cell, and no raw newline."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;").replace("\n", "&#10;")
    )


def value_cell(value: Any) -> str:
    """The full value: inline if short, else pretty-printed in a <details> block. Keys are sorted; nothing is cut."""
    compact = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
    if len(compact) <= INLINE_LIMIT:
        return f"<code>{html(compact)}</code>"
    pretty = json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
    return f"<details><summary>{pretty.count(chr(10)) + 1} lines</summary><pre>{html(pretty)}</pre></details>"


def sequence(hops: list[dict[str, Any]], shapes: dict[str, str]) -> str:
    """The Mermaid sequenceDiagram: a participant per brick (first-touched order) and per source port, one arrow per hop."""
    bricks: list[str] = []
    ports: list[str] = []
    for hop in hops:
        for element in (hop["from"], hop["to"]):
            target = ports if "." in element else bricks
            if element not in target:
                target.append(element)
    ids = {name: f"p{index}" for index, name in enumerate(bricks, start=1)}
    ids.update({name: f"s{index}" for index, name in enumerate(ports, start=1)})
    lines = ["sequenceDiagram"]
    for name in bricks:
        lines.append("    participant %s as %s" % (ids[name], mermaid_escape(f"{name} ({shapes[name]})")))
    for name in ports:
        lines.append("    participant %s as %s" % (ids[name], mermaid_escape(name + " (source)")))
    for hop in hops:
        start, end, n, how = ids[hop["from"]], ids[hop["to"]], hop["n"], hop["how"]
        if hop["kind"] in ("input", "output"):
            lines.append("    Note over %s: %s" % (start, mermaid_escape(f"{n} {how}")))
        elif hop["kind"] == "joint":
            text = how.replace(f"joint from {hop['from']}, ", f"joint {hop['to']}: ", 1)
            lines.append("    %s->>%s: %s" % (start, end, mermaid_escape(f"{n} {text}")))
        elif hop["kind"] == "source":
            port = hop["to"].split(".", 1)[1]
            detail = how.removeprefix("source ")
            take = hop["take"] or "none"
            lines.append("    %s->>%s: %s" % (start, end, mermaid_escape(f"{n} {port}: {detail}, take {take}")))
        else:
            lines.append("    %s-->>%s: %s" % (start, end, mermaid_escape(f"{n} {how}")))
    return "\n".join(lines)


def markdown(brick: str, name: str, mode: str, hops: list[dict[str, Any]], shapes: dict[str, str]) -> str:
    rows = []
    for hop in hops:
        value = value_cell(hop["value"])
        if "request" in hop:
            value = f"request: {value_cell(hop['request'])}<br>response: {value}"
        take = f"<code>{html(hop['take'])}</code>" if hop["take"] else "none"
        rows.append(f"| {hop['n']} | {html(hop['where'])} | {html(hop['how'])} | {take} | {value} |")
    return "\n".join(
        [
            f"# Transcript report: `{brick}` / `{name}`",
            "",
            f"- Entry brick: `{brick}` ({shapes[brick]})",
            f"- Mode: {mode}",
            f"- Hops: {len(hops)}",
            "",
            "## Sequence",
            "",
            "```mermaid",
            sequence(hops, shapes),
            "```",
            "",
            "## Hops",
            "",
            "| # | where | how it got here | tape take | value |",
            "|---|---|---|---|---|",
            *rows,
            "",
        ]
    )


def mode_text(fresh: Any, record: Any) -> tuple[str, dict[str, list[str]]]:
    def names(option: Any) -> list[str]:
        if option is False or option is None:
            return []
        return ["(every source of the entry brick)"] if option is True else sorted(set(option))

    plan = {"fresh": names(fresh), "record": names(record)}
    if not plan["fresh"] and not plan["record"]:
        return "replay (every source answered from its tape; nothing went to a live source)", plan
    parts = [f"{label}: {', '.join(plan[label])}" for label in ("fresh", "record") if plan[label]]
    return "; ".join(parts) + " (everything else replayed)", plan


def ports(option: list[str] | None) -> Any:
    """False when the flag is absent, True when it has no names, else the names."""
    return False if option is None else option or True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("brick", help="folder name under bricks/: the brick the sample enters")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the project (default: the current directory)")
    parser.add_argument("--input", default="-", help="JSON file with the sample input record, or - for stdin (default)")
    parser.add_argument("--name", help="name of the sample, used in the file names (default: the input file's name, or stdin)")
    parser.add_argument("--out", type=Path, help="folder for the report (default: <root>/reports/transcripts)")
    parser.add_argument("--fresh", nargs="*", metavar="PORT", help="go to the live source for all sources, or the named ports")
    parser.add_argument("--record", nargs="*", metavar="PORT", help="record all sources, or the named ports (implies fresh)")
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
    stem = args.name or ("stdin" if args.input == "-" else Path(args.input).stem)
    name = re.sub(r"[^a-z0-9_-]+", "-", stem.lower()).strip("-") or "sample"

    fresh, record = ports(args.fresh), ports(args.record)
    tracer, error = trace(root, args.brick, inputs, fresh, record)
    shapes = {brick: tracer.shape(brick) for brick in {args.brick, *(h["from"].split(".")[0] for h in tracer.hops), *(h["to"].split(".")[0] for h in tracer.hops)}}
    mode, plan = mode_text(fresh, record)

    out = (args.out or root / "reports" / "transcripts").resolve()
    out.mkdir(parents=True, exist_ok=True)
    document = {"brick": args.brick, "name": name, "mode": plan, "shapes": dict(sorted(shapes.items())), "hops": tracer.hops}
    (out / f"{args.brick}-{name}.json").write_bytes((json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (out / f"{args.brick}-{name}.md").write_bytes(markdown(args.brick, name, mode, tracer.hops, shapes).encode("utf-8"))
    try:
        shown = out.relative_to(root).as_posix()
    except ValueError:
        shown = out.as_posix()
    print(f"wrote {shown}/{args.brick}-{name}.md and .json ({len(tracer.hops)} hops)")
    if error:
        print(f"error: the run failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
