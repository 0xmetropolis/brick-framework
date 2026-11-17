#!/usr/bin/env python3
"""Show samples of the payloads a brick project passes around, grouped by brick. Standard library only.

    python3 tools/transcript_brick.py <brick> [--root PROJECT] [--input FILE|-] [--name NAME]
                                      [--fresh [PORT ...]] [--record [PORT ...]] [--out DIR]

``<brick>`` is the brick a sample enters, usually an entry brick, and ``--input`` is one JSON input
record for it (a file, or stdin). The brick is run once through its own ``run``, replaying every
source from its tape; nothing goes to a live source unless you ask with ``--fresh`` or ``--record``
(port names as for ``run_brick.py``). The report has one section per brick of the project, in name
order, and each section shows what that brick handles:

* **Run**: the brick's own ``run`` calls during this sample, each with its input and its output, and
  who called it (the sample itself, or a sibling over a joint).
* **Joints**: for each joint the brick declares, what crossed to the sibling and what came back.
* **Ports**: for each source port the brick declares, requests and responses taken from the takes on
  its tape, whichever run recorded them.

Not every payload is shown: a section shows at most three samples of each kind, chosen by a fixed rule.
Take the candidates (the calls, the crossings, or the takes), measure each one as the size in bytes
of its canonical JSON (sorted keys, no spaces), and order them by that size, then by that text, then
by their order. With one candidate it is shown as ``only``; with two, ``smallest`` and ``largest``;
with three or more, ``smallest``, ``typical`` (the lower median) and ``largest``. A payload bigger
than 1000 bytes is trimmed to its top-level keys, each with its type, its size and one short sample
value; its full size is stated. Nothing else is cut.

Two files are written, with the same content:

    <out>/<brick>-<name>.md     for reading
    <out>/<brick>-<name>.json   for tests and tools

``--out`` defaults to ``<root>/reports/transcripts`` and ``--name`` to the input file's name. The
report carries no run ID, timestamp or path of this machine, and replaying the same project and input
gives the same bytes. The run's ledger entry is local and gitignored (``BRICK_TEST=1``).
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
else:
    from .brick_contract import brick_ports, brick_shape
    from .graph_bricks import label_text

HOOKED = re.compile(r"bricks\.([a-z][a-z0-9_]*)\.runner\.run")
# A payload bigger than this many bytes of canonical JSON is trimmed to its top-level keys.
TRIM_BYTES = 1000
# The most samples shown of one kind.
SAMPLE_LABELS = {1: ("only",), 2: ("smallest", "largest"), 3: ("smallest", "typical", "largest")}
STRING_SAMPLE = 40


def plain(value: Any) -> Any:
    """A value as plain JSON data: tuples are lists, bytes are a base64 object, anything else is its repr."""

    def encode(item: Any) -> Any:
        if isinstance(item, (bytes, bytearray)):
            return {"base64": base64.b64encode(bytes(item)).decode("ascii")}
        return repr(item)

    return json.loads(json.dumps(value, default=encode))


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def size_of(value: Any) -> int:
    """The size in bytes of a value's canonical JSON: the measure samples are chosen and trimmed by."""
    return len(canonical(value).encode("utf-8"))


def kind_of(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "list"
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    return "number"


def scalar_sample(value: Any) -> str:
    """A scalar as it reads in a sample: numbers, booleans and null whole, a long string cut with its length."""
    if isinstance(value, str) and len(value) > STRING_SAMPLE:
        return json.dumps(value[:STRING_SAMPLE], ensure_ascii=False)[:-1] + f"…\" ({len(value)} chars)"
    return json.dumps(value, ensure_ascii=False)


def sample_of(value: Any) -> str:
    """One short sample value of ``value``: itself if a scalar, else its first element or entry."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        key = next(iter(sorted(value)))
        inner = value[key]
        return f"{json.dumps(key, ensure_ascii=False)}: " + (scalar_sample(inner) if not isinstance(inner, (dict, list)) else describe_short(inner))
    if isinstance(value, list):
        if not value:
            return "[]"
        inner = value[0]
        return scalar_sample(inner) if not isinstance(inner, (dict, list)) else describe_short(inner)
    return scalar_sample(value)


def describe_short(value: Any) -> str:
    """A nested container as its type and size, one level, with no sample of its own."""
    count = len(value)
    return f"{kind_of(value)} of {count} {'keys' if isinstance(value, dict) else 'items'}"


def describe(value: Any) -> dict[str, Any]:
    """One top-level value of a trimmed payload: its type, its size if it is a container, and one sample value."""
    entry: dict[str, Any] = {"type": kind_of(value)}
    if isinstance(value, (dict, list)):
        entry["size"] = len(value)
    entry["sample"] = sample_of(value)
    return entry


def present(value: Any) -> dict[str, Any]:
    """A payload as shown: whole with its size when small, else trimmed to its top-level keys with the full size."""
    value = plain(value)
    size = size_of(value)
    if size <= TRIM_BYTES:
        return {"bytes": size, "value": value}
    if isinstance(value, dict):
        keys = {key: describe(inner) for key, inner in sorted(value.items())}
    else:
        keys = {"(the whole value)": describe(value)}
    return {"bytes": size, "top_level_keys": keys}


def choose(candidates: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """The samples shown from a list of candidates, each with its label; see the module docstring for the rule.

    A candidate is a dict with a ``size`` and a ``text`` (its canonical JSON); ties on size are broken by text,
    then by the order the candidates came in.
    """
    ordered = sorted(enumerate(candidates), key=lambda pair: (pair[1]["size"], pair[1]["text"], pair[0]))
    items = [item for _, item in ordered]
    if len(items) <= 3:
        picked = items
    else:
        picked = [items[0], items[(len(items) - 1) // 2], items[-1]]
    return list(zip(SAMPLE_LABELS[len(picked)], picked)) if picked else []


class Tracer:
    """Collects the ``run`` calls of one sample, in order, from a hook on each brick's runner module."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, Any]] = []
        self.stack: list[str] = []

    def patch_runner(self, brick: str, module: Any) -> None:
        tracer, real = self, module.execute

        def execute(inputs: Any, context: Any) -> Any:
            call = {"brick": brick, "from": tracer.stack[-1] if tracer.stack else None, "input": plain(inputs)}
            tracer.calls.append(call)
            tracer.stack.append(brick)
            try:
                output = real(inputs, context)
            except BaseException as exc:
                call["error"] = {"type": type(exc).__name__, "message": str(exc)}
                raise
            finally:
                tracer.stack.pop()
            call["output"] = plain(output)
            return output

        module.execute = execute


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
        spec.loader = _Loader(spec.loader, lambda module, brick=match.group(1): self.tracer.patch_runner(brick, module))
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


def call_candidate(call: dict[str, Any]) -> dict[str, Any]:
    outcome = {"error": call["error"]} if "error" in call else {"output": call.get("output")}
    payload = {"input": call["input"], **outcome}
    return {"size": size_of(payload), "text": canonical(payload), "call": call}


def read_takes(root: Path, brick: str, source: str) -> tuple[list[dict[str, Any]], int]:
    """The takes on a source's tape as candidates, and how many files were not readable takes."""
    folder = root / "bricks" / brick / "input" / "tapes" / source
    candidates: list[dict[str, Any]] = []
    skipped = 0
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped += 1
            continue
        if not isinstance(record, dict) or "request" not in record or ("response" in record) == ("error" in record):
            skipped += 1
            continue
        payload = {"request": record["request"], **({"response": record["response"]} if "response" in record else {"error": record["error"]})}
        candidates.append({"size": size_of(payload), "text": canonical(payload) + path.name, "take": path.relative_to(root).as_posix(), "payload": payload})
    return candidates, skipped


def selected(candidates: list[dict[str, Any]], shape: Any) -> list[dict[str, Any]]:
    """The labelled samples of a list of candidates, each as ``shape(item)`` plus its label and its size."""
    return [{"label": label, "bytes": item["size"], **shape(item)} for label, item in choose(candidates)]


def build(root: Path, entry: str, tracer: Tracer, error: str | None, name: str, plan: dict[str, list[str]]) -> dict[str, Any]:
    """The report as plain data: one entry per brick of the project, in name order."""
    names = sorted(path.name for path in (root / "bricks").iterdir() if path.is_dir() and not path.name.startswith((".", "__")))
    bricks = []
    for brick in names:
        folder = root / "bricks" / brick
        ports = brick_ports(folder)
        own = [call_candidate(call) for call in tracer.calls if call["brick"] == brick]

        def run_sample(item: dict[str, Any]) -> dict[str, Any]:
            call = item["call"]
            sample: dict[str, Any] = {"from": call["from"] or "sample input", "input": present(call["input"])}
            sample["error" if "error" in call else "output"] = present(call["error"] if "error" in call else call.get("output"))
            return sample

        joints = []
        for joint, port in sorted(ports.items()):
            if port.get("kind") != "joint":
                continue
            crossings = [call_candidate(call) for call in tracer.calls if call["brick"] == joint and call["from"] == brick]
            joints.append({
                "name": joint, "policy": port.get("policy"), "pin": port.get("pin"), "follow": list(port.get("follow") or ()),
                "crossings": len(crossings), "samples": selected(crossings, run_sample),
            })
        sources = []
        for source, port in sorted(ports.items()):
            if port.get("kind") != "source":
                continue
            takes, skipped = read_takes(root, brick, source)

            def take_sample(item: dict[str, Any]) -> dict[str, Any]:
                payload = item["payload"]
                sample: dict[str, Any] = {"take": item["take"], "request": present(payload["request"])}
                sample["error" if "error" in payload else "response"] = present(payload["error"] if "error" in payload else payload["response"])
                return sample

            sources.append({
                "name": source, "epoch": port.get("epoch"), "label": port.get("label") or {}, "followable": bool(port.get("followable")),
                "takes": len(takes), "unreadable": skipped, "samples": selected(takes, take_sample),
            })
        bricks.append({
            "name": brick, "shape": brick_shape(folder),
            "run": {"calls": len(own), "samples": selected(own, run_sample)},
            "joints": joints, "ports": sources,
        })
    document: dict[str, Any] = {"brick": entry, "name": name, "mode": plan, "trim_bytes": TRIM_BYTES, "bricks": bricks}
    if error:
        document["error"] = error
    return document


def show(payload: dict[str, Any]) -> list[str]:
    """A payload as Markdown lines: pretty JSON when whole, else one line per top-level key."""
    if "value" in payload:
        return ["```json", *json.dumps(payload["value"], sort_keys=True, ensure_ascii=False, indent=2).splitlines(), "```"]
    keys = payload["top_level_keys"]
    width = max(len(key) for key in keys)
    lines = [f"trimmed to its top-level keys (full size {payload['bytes']:,} bytes)", "", "```text"]
    for key, entry in keys.items():
        shape = entry["type"] + (f" of {entry['size']:,} {'keys' if entry['type'] == 'object' else 'items'}" if "size" in entry else "")
        lines.append(f"{key.ljust(width)}  {shape}, e.g. {entry['sample']}")
    lines.append("```")
    return lines


def labelled(title: str, payload: dict[str, Any]) -> list[str]:
    """A titled payload: its name and size, then the payload."""
    return [f"**{title}** ({payload['bytes']:,} bytes)", "", *show(payload), ""]


def markdown(document: dict[str, Any]) -> str:
    plan = document["mode"]
    if not plan["fresh"] and not plan["record"]:
        mode = "replay (every source answered from its tape; nothing went to a live source)"
    else:
        mode = "; ".join(f"{label}: {', '.join(plan[label])}" for label in ("fresh", "record") if plan[label]) + " (everything else replayed)"
    shapes = {brick["name"]: brick["shape"] for brick in document["bricks"]}
    lines = [
        f"# Payload samples: `{document['brick']}` / `{document['name']}`",
        "",
        f"- Sample enters: `{document['brick']}` ({shapes.get(document['brick'], 'open')})",
        f"- Mode: {mode}",
        f"- Bricks: {len(document['bricks'])}",
        "- Samples: at most three of each kind, `smallest`, `typical` (lower median) and `largest` by the size of their canonical JSON; "
        f"a payload over {document['trim_bytes']:,} bytes is trimmed to its top-level keys.",
    ]
    if "error" in document:
        lines.append(f"- The run failed: `{document['error']}`")
    lines.append("")
    for brick in document["bricks"]:
        lines += [f"## {brick['name']} ({brick['shape']})", "", "### Run", ""]
        run = brick["run"]
        if not run["calls"]:
            lines += ["Not called by this sample.", ""]
        else:
            lines += [f"{run['calls']} call{'s' if run['calls'] != 1 else ''} during this sample; {len(run['samples'])} shown.", ""]
            for sample in run["samples"]:
                lines += [f"#### {sample['label']}: from {sample['from']}", ""]
                lines += labelled("input", sample["input"])
                lines += labelled("error" if "error" in sample else "output", sample.get("error") or sample["output"])
        lines += ["### Joints", ""]
        if not brick["joints"]:
            lines += ["None declared.", ""]
        for joint in brick["joints"]:
            follows = f", follows {', '.join(joint['follow'])}" if joint["follow"] else ""
            lines += [f"#### to `{joint['name']}` ({joint['policy']}, pin v{joint['pin']}{follows})", ""]
            if not joint["crossings"]:
                lines += ["Not crossed by this sample.", ""]
                continue
            lines += [f"{joint['crossings']} crossing{'s' if joint['crossings'] != 1 else ''}; {len(joint['samples'])} shown.", ""]
            for sample in joint["samples"]:
                lines += [f"##### {sample['label']}", ""]
                lines += labelled("crossed", sample["input"])
                lines += labelled("came back" if "output" in sample else "failed", sample.get("output") or sample["error"])
        lines += ["### Ports", ""]
        if not brick["ports"]:
            lines += ["None declared.", ""]
        for port in brick["ports"]:
            label = label_text(port["label"])
            details = f"epoch {port['epoch']}" + (f", label {label}" if label else "") + (", followable" if port["followable"] else "")
            lines += [f"#### source `{port['name']}` ({details})", ""]
            note = f"{port['takes']} take{'s' if port['takes'] != 1 else ''} on the tape; {len(port['samples'])} shown."
            if port["unreadable"]:
                note += f" {port['unreadable']} file{'s' if port['unreadable'] != 1 else ''} in the folder {'are' if port['unreadable'] != 1 else 'is'} not a readable take."
            lines += [note, ""]
            for sample in port["samples"]:
                lines += [f"##### {sample['label']}: `{sample['take']}`", ""]
                lines += labelled("request", sample["request"])
                lines += labelled("error" if "error" in sample else "response", sample.get("error") or sample["response"])
    return "\n".join(lines).rstrip("\n") + "\n"


def mode_plan(fresh: Any, record: Any) -> dict[str, list[str]]:
    def names(option: Any) -> list[str]:
        if option is False or option is None:
            return []
        return ["(every source of the entry brick)"] if option is True else sorted(set(option))

    return {"fresh": names(fresh), "record": names(record)}


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
    document = build(root, args.brick, tracer, error, name, mode_plan(fresh, record))

    out = (args.out or root / "reports" / "transcripts").resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.brick}-{name}.json").write_bytes((json.dumps(document, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (out / f"{args.brick}-{name}.md").write_bytes(markdown(document).encode("utf-8"))
    try:
        shown = out.relative_to(root).as_posix()
    except ValueError:
        shown = out.as_posix()
    print(f"wrote {shown}/{args.brick}-{name}.md and .json ({len(document['bricks'])} bricks)")
    if error:
        print(f"error: the run failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
