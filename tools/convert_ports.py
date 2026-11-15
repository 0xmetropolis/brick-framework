#!/usr/bin/env python3
"""One-time conversion of a project's bricks to the ports format. Standard library only.

    python3 tools/convert_ports.py [--root PATH] [--dry-run]

Run it once, as part of moving a project to this version of the framework. There is no
compatibility mode: after the framework is updated, a brick in the old format fails the
lint with a pointer here, and a take or manifest in the old format is never read. This
script is the only reader the old format has, and it is not part of what a brick uses at
run time; delete it from the project once the conversion is committed.

For every brick under ``<root>/bricks`` that is still in the old format it:

* rewrites ``contract.py``: ``SIBLING_DEPENDENCIES``, ``LIVE_ADAPTER``, ``MODE_PASSING`` and
  ``LANE`` become one ``PORTS`` (a source port per outside-source adapter, with ``epoch = 1``,
  a ``label`` taken from the takes' old ``fingerprint``, and ``followable`` when the brick
  was a Live Adapter; a joint port per sibling adapter, with the sibling's current
  ``CONTRACT_VERSION`` as its ``pin`` and ``follow`` where the old manifest passed the mode);
* converts every take in ``input/data/<adapter>/<case>.json`` to schema 2 under
  ``input/tapes/<source>/<take>.json`` (``adapter`` to ``source``, ``case`` to ``take``,
  ``capture_run_id`` to ``recorded_in``, ``fingerprint`` to ``label``, the brick version
  dropped, ``epoch`` added) and removes the old files;
* moves ``runner/runs`` to ``runner/ledger``: a tracked old record that went fresh, was saved
  or failed becomes a tracked entry, every other one a local ``.local.json`` entry;
* replaces the boilerplate (``input/evidence.py`` by ``input/tape.py``, ``runner/run.py``,
  ``runner/rng.py``) with this framework's copies, and renames the keys in ``input/config.yml``.

It cannot edit the calls inside your adapters and ``src``: it lists every remaining use of
an old name (``through``, ``Transcript``, ``pass_mode``, ``save=``, ``BRICK_RUN_KIND``, ...)
with its file and line, and says what to use instead. The linter then holds the rest.
``--dry-run`` converts a copy and prints the same report without touching the project.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from brick_contract import assigned_literal  # type: ignore[no-redef]
else:
    from .brick_contract import assigned_literal

FRAMEWORK = Path(__file__).resolve().parents[1]
TEMPLATE = FRAMEWORK / "bricks" / "example_brick"
OLD_DECLARATIONS = ("SIBLING_DEPENDENCIES", "LIVE_ADAPTER", "MODE_PASSING", "LANE")
BOILERPLATE = ("input/tape.py", "runner/run.py", "runner/rng.py")
CONFIG_KEYS = {
    "runs": "entries",
    "saved_examples_per_adapter": "takes_per_tape",
    "max_evidence_bytes": "max_take_bytes",
    "max_run_record_bytes": "max_entry_bytes",
}
# Calls that name an adapter (the tape folder) and where the name sits among the arguments.
OLD_CALLS = {"through": 1, "Transcript": 1, "load_example": 0, "save_example": 0}
OLD_NAMES = (
    (re.compile(r"\bthrough\("), "through() is tape.once(ctx, '<source>', take, request, fetch)"),
    (re.compile(r"\bTranscript\("), "Transcript is tape.session(ctx, '<source>', name); the fingerprint moved to the port's label"),
    (re.compile(r"\bload_example\b|\bsave_example\b"), "load_example/save_example are tape.load/tape.write"),
    (re.compile(r"\bpass_mode\b|\bfresh_siblings\b"), "pass_mode is tape.follow(ctx, '<joint>') with its flags fresh and record"),
    (re.compile(r"\bsave\s*="), "save= is record="),
    (re.compile(r"""\[['"]mode['"]\]|\.get\(['"]mode['"]"""), "the run context has a plan, not a mode (tape.mode(ctx, '<source>'))"),
    (re.compile(r"BRICK_RUN_KIND"), "BRICK_RUN_KIND is BRICK_TEST=1"),
    (re.compile(r"\bcapture_run_id\b|\bprovenance\b|\bcheck_source\b|\bSourceMismatch\b|\bEvidenceError\b"),
     "capture_run_id, provenance, check_source and EvidenceError are recorded_in, tape.stamp, tape.verify and TapeError"),
    (re.compile(r"from \.\.evidence|from \.evidence|import evidence"), "evidence.py is tape.py"),
)

# Kept in step with the template's contract.py (a test compares them).
VERSION_COMMENT = (
    "# The interface version: bump it when BrickInput or BrickOutput changes meaning.\n"
    "# Joints in other bricks pin the version they were written against.\n"
)
PORTS_COMMENT = '''# Every crossing of the brick boundary, declared once and implemented by one file
# of the same name under input/adapters/. Nothing else says what kind of brick this
# is: the tools compute its shape from these ports and its files. No ports (PORTS = {},
# no input/adapters/ or input/tapes/) is a sealed brick; a __main__.py makes an entry
# brick; everything else is open.
#
# A source port reaches an outside system.
#   epoch      bumped when the adapter's request or response meaning changes; takes
#              recorded at another epoch are stale
#   label      optional: what the source is (a version, a build, a protocol), merged
#              into every take's request and checked on connect; a session needs one
#   followable optional: a caller may ask this source to go fresh
# A joint port reaches a sibling brick. It is named for the sibling.
#   policy     "eventual" or "orchestrated"
#   pin        the sibling's CONTRACT_VERSION this brick was written against
#   follow     optional, orchestrated only: the sibling's followable sources that this
#              brick's fresh and record reach
'''
OWNED_STATE_OLD = "# Brick-local evidence under input/data and runner/runs is owned implicitly.\n"
OWNED_STATE_NEW = "# Brick-local takes under input/tapes and entries under runner/ledger are owned implicitly.\n"


class Report:
    """What the conversion did, and what is left for a person."""

    def __init__(self) -> None:
        self.done: list[str] = []
        self.notes: list[str] = []
        self.manual: list[str] = []

    def say(self, brick: str, message: str) -> None:
        self.done.append(f"{brick}: {message}")

    def note(self, brick: str, message: str) -> None:
        self.notes.append(f"{brick}: {message}")


def is_old(brick: Path) -> bool:
    """A brick is in the old format if any trace of it is left."""
    tree = parse(brick / "contract.py")
    names = {
        target.id
        for node in (tree.body if tree else ())
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else [])
        if isinstance(target, ast.Name)
    }
    return bool(
        names & set(OLD_DECLARATIONS)
        or (tree is not None and "PORTS" not in names)
        or (brick / "input/evidence.py").exists()
        or (brick / "input/data").exists()
        or (brick / "runner/runs").exists()
    )


def parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None


def called(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    return node.func.attr if isinstance(node.func, ast.Attribute) else ""


def classify(brick: Path) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """The old brick's adapters as ``(sources, joints)``.

    ``sources`` maps a source name (the adapter file's stem) to ``{"folders": {...}, "label": ...}``;
    ``joints`` maps a sibling name to the adapter file stem that imports it.
    """
    sources: dict[str, dict[str, Any]] = {}
    joints: dict[str, str] = {}
    for path in sorted((brick / "input/adapters").glob("*.py")) if (brick / "input/adapters").is_dir() else ():
        tree = parse(path)
        if tree is None or path.name == "__init__.py":
            continue
        sibling = None
        folders: set[str] = set()
        label = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.startswith("bricks."):
                sibling = node.module.split(".")[1]
            if isinstance(node, ast.Call) and called(node) in OLD_CALLS:
                position = OLD_CALLS[called(node)]
                if len(node.args) > position and isinstance(node.args[position], ast.Constant) and isinstance(node.args[position].value, str):
                    folders.add(node.args[position].value)
                for keyword in node.keywords:
                    if keyword.arg == "fingerprint":
                        try:
                            label = ast.literal_eval(keyword.value)
                        except (TypeError, ValueError):
                            pass
        if sibling:
            joints[sibling] = path.stem
        else:
            sources[path.stem] = {"folders": folders or {path.stem}, "label": label if isinstance(label, dict) and label else None}
    return sources, joints


def fingerprints(brick: Path, folders: set[str]) -> list[Any]:
    """The distinct request fingerprints of the old takes in the given data folders."""
    found: list[Any] = []
    for folder in sorted(folders):
        for path in sorted((brick / "input/data" / folder).glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8")).get("request")
            except (OSError, ValueError):
                continue
            value = request.get("fingerprint") if isinstance(request, dict) else None
            if value not in found:
                found.append(value)
    return found


def ports_text(ports: dict[str, dict[str, Any]]) -> str:
    if not ports:
        return "PORTS: dict[str, dict] = {}\n"
    lines = ["PORTS: dict[str, dict] = {"]
    for name, port in ports.items():
        fields = ", ".join(f"{key!r}: {value!r}".replace("'", '"') for key, value in port.items())
        lines.append(f'    "{name}": {{{fields}}},')
    lines.append("}")
    return "\n".join(lines) + "\n"


def rewrite_contract(brick: Path, ports: dict[str, dict[str, Any]]) -> None:
    """Drop the old declarations (and the comment block above each), declare PORTS."""
    path = brick / "contract.py"
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    remove: list[tuple[int, int]] = []
    owned_line = class_line = version_line = None
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        name = next((target.id for target in targets if isinstance(target, ast.Name)), "")
        start = node.lineno - 1
        if name in OLD_DECLARATIONS:
            while start > 0 and lines[start - 1].lstrip().startswith("#"):
                start -= 1
            end = node.end_lineno
            if end < len(lines) and not lines[end].strip():
                end += 1
            remove.append((start, end))
        elif name == "OWNED_STATE" and owned_line is None:
            owned_line = start
        elif name == "CONTRACT_VERSION" and version_line is None:
            version_line = start
        elif isinstance(node, ast.ClassDef) and node.name == "BrickInput" and class_line is None:
            class_line = start
    block = PORTS_COMMENT + ports_text(ports) + "\n"
    insert_at = None
    if owned_line is not None:
        insert_at = owned_line
        while insert_at > 0 and lines[insert_at - 1].lstrip().startswith("#"):
            insert_at -= 1
    elif class_line is not None:
        insert_at = class_line
    edits: list[tuple[int, int, str]] = [(a, b, "") for a, b in remove]
    if insert_at is not None:
        edits.append((insert_at, insert_at, block))
    else:
        edits.append((len(lines), len(lines), "\n" + block))
    if version_line is not None and not (version_line > 0 and lines[version_line - 1].lstrip().startswith("#")):
        edits.append((version_line, version_line, VERSION_COMMENT))
    for a, b, replacement in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        lines[a:b] = [replacement] if replacement else []
    result = "".join(lines).replace(OWNED_STATE_OLD, OWNED_STATE_NEW)
    path.write_text(result, encoding="utf-8")


def convert_takes(brick: Path, folder_map: dict[str, str], report: Report, name: str) -> int:
    """Move every old take to its tape folder in schema 2. Returns how many."""
    data = brick / "input/data"
    count = 0
    for path in sorted(data.rglob("*.json")) if data.is_dir() else ():
        relative = path.relative_to(data)
        if len(relative.parts) != 2:
            report.note(name, f"{path.relative_to(brick)} is not <adapter>/<case>.json; left alone")
            continue
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            report.note(name, f"{path.relative_to(brick)} is not valid JSON ({exc}); left alone")
            continue
        if not isinstance(old, dict) or old.get("schema_version") != 1:
            report.note(name, f"{path.relative_to(brick)} is not a schema 1 take; left alone")
            continue
        source = folder_map.get(relative.parts[0], relative.parts[0])
        take = old.get("case") or path.stem
        request = old.get("request") if isinstance(old.get("request"), dict) else {}
        if "fingerprint" in request:
            request = {("label" if key == "fingerprint" else key): value for key, value in request.items()}
        new: dict[str, Any] = {
            "epoch": 1,
            "recorded_in": old.get("capture_run_id"),
            "request": request,
            "schema_version": 2,
            "source": source,
            "take": take,
        }
        for key in ("response", "error"):
            if key in old:
                new[key] = old[key]
        target = brick / "input/tapes" / source / f"{take}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.unlink()
        count += 1
    if data.is_dir():
        keep = data / ".gitkeep"
        if keep.exists():
            (brick / "input/tapes").mkdir(parents=True, exist_ok=True)
            shutil.move(str(keep), str(brick / "input/tapes/.gitkeep"))
        for folder in sorted(data.rglob("*"), reverse=True):
            if folder.is_dir():
                try:
                    folder.rmdir()
                except OSError:
                    pass
        try:
            data.rmdir()
        except OSError:
            report.note(name, "input/data still holds files that are not takes; move or delete them")
    return count


def convert_ledger(brick: Path, sources: list[str], report: Report, name: str) -> None:
    """runner/runs to runner/ledger, one entry per old record, tracked only if it went fresh or failed."""
    runs = brick / "runner/runs"
    if not runs.is_dir():
        return
    ledger = brick / "runner/ledger"
    ledger.mkdir(parents=True, exist_ok=True)
    tracked = local = 0
    for path in sorted(runs.glob("*.json")):
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink()
            continue
        is_test = path.name.endswith(".test.json") or old.get("kind") == "test"
        mode = old.get("mode")
        entry = {key: value for key, value in old.items() if key not in {"kind", "mode", "fresh_siblings"}}
        entry["fresh"] = sorted(sources) if mode in ("fresh", "save") else []
        entry["record"] = sorted(sources) if mode == "save" else []
        entry["followed"] = []
        run_id = str(old.get("run_id") or path.name.split(".")[0])
        touched = bool(entry["fresh"] or "error" in entry)
        suffix = ".json" if touched and not is_test else ".local.json"
        (ledger / f"{run_id}{suffix}").write_text(json.dumps(entry, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.unlink()
        tracked, local = tracked + (suffix == ".json"), local + (suffix != ".json")
    (ledger / ".gitignore").write_text("*.local.json\n", encoding="utf-8")
    keep = runs / ".gitkeep"
    if keep.exists():
        shutil.move(str(keep), str(ledger / ".gitkeep"))
    (runs / ".gitignore").unlink(missing_ok=True)
    try:
        runs.rmdir()
    except OSError:
        report.note(name, "runner/runs still holds files that are not records; move or delete them")
    report.say(name, f"moved runner/runs to runner/ledger ({tracked} tracked entries, {local} local)")


def convert_config(brick: Path) -> None:
    path = brick / "input/config.yml"
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    for old, new in CONFIG_KEYS.items():
        text = re.sub(rf"(?m)^(\s*){re.escape(old)}:", rf"\1{new}:", text)
    path.write_text(text, encoding="utf-8")


def scan(brick: Path, name: str, report: Report) -> None:
    """List the old names still used by the brick's own code."""
    for path in sorted(brick.rglob("*.py")):
        relative = path.relative_to(brick).as_posix()
        if relative in BOILERPLATE or relative == "contract.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            for pattern, advice in OLD_NAMES:
                if pattern.search(code):
                    report.manual.append(f"bricks/{name}/{relative}:{number}: {line.strip()}   ->  {advice}")
                    break


def convert_brick(root: Path, brick: Path, report: Report) -> None:
    name = brick.name
    tree = parse(brick / "contract.py")
    if tree is None:
        report.note(name, "contract.py is missing or not valid Python; skipped")
        return
    deps = assigned_literal(tree, "SIBLING_DEPENDENCIES")
    deps = deps if isinstance(deps, dict) else {}
    live = assigned_literal(tree, "LIVE_ADAPTER") is True
    passing = assigned_literal(tree, "MODE_PASSING")
    passing = tuple(passing) if isinstance(passing, (tuple, list)) else ()
    lane = assigned_literal(tree, "LANE")

    sources, joints = classify(brick)
    ports: dict[str, dict[str, Any]] = {}
    folder_map: dict[str, str] = {}
    for source, found in sources.items():
        port: dict[str, Any] = {"kind": "source", "epoch": 1}
        distinct = fingerprints(brick, found["folders"])
        label = distinct[0] if len(distinct) == 1 and isinstance(distinct[0], dict) else None
        if len(distinct) > 1:
            report.note(name, f"source {source!r}: its takes carry different fingerprints; no label was set, re-record them")
        elif not distinct and found["label"]:
            label = found["label"]
        if label:
            port["label"] = label
        if live:
            port["followable"] = True
        ports[source] = port
        for folder in found["folders"]:
            folder_map[folder] = source
            if folder != source:
                report.note(
                    name,
                    f"adapter {source}.py names its tape {folder!r}: the takes moved to tapes/{source}/, "
                    f"and the call must now name {source!r}",
                )
    claimed = {folder for found in sources.values() for folder in found["folders"]}
    for folder in sorted(p.name for p in (brick / "input/data").iterdir() if p.is_dir()) if (brick / "input/data").is_dir() else ():
        if folder not in claimed:
            report.note(name, f"no adapter file claims the old data folder {folder!r}; its takes moved to tapes/{folder}/ and the lint will flag the folder")
    for sibling in sorted(set(deps) | set(joints)):
        policy = deps.get(sibling) if deps.get(sibling) in ("eventual", "orchestrated") else "eventual"
        if sibling not in deps:
            report.note(name, f"adapter imports bricks.{sibling} but the manifest did not declare it; declared as eventual")
        pin = assigned_literal(parse(root / "bricks" / sibling / "contract.py") or ast.parse(""), "CONTRACT_VERSION")
        if not isinstance(pin, int) or isinstance(pin, bool) or pin < 1:
            report.note(name, f"joint {sibling!r}: no CONTRACT_VERSION found for bricks/{sibling}; pinned at 1")
            pin = 1
        port = {"kind": "joint", "policy": policy, "pin": pin}
        if sibling in passing:
            callee_sources, _ = classify(root / "bricks" / sibling)
            if callee_sources:
                port["follow"] = tuple(sorted(callee_sources))
            else:
                report.note(name, f"joint {sibling!r} passed its mode but {sibling!r} has no source adapters; no follow was declared")
        ports[sibling] = port
        if sibling in joints and joints[sibling] != sibling:
            current, wanted = brick / "input/adapters" / f"{joints[sibling]}.py", brick / "input/adapters" / f"{sibling}.py"
            if not wanted.exists():
                current.rename(wanted)
                report.say(name, f"renamed adapter {current.name} to {wanted.name} (a joint's adapter is named for its sibling)")
            else:
                report.note(name, f"{current.name} imports bricks.{sibling}; rename it to {wanted.name}")
        elif sibling not in joints:
            report.note(name, f"joint {sibling!r} has no adapter file importing bricks.{sibling}; the lint will say so")

    rewrite_contract(brick, ports)
    report.say(name, f"declared PORTS: {', '.join(f'{k} ({v['kind']})' for k, v in ports.items()) or 'none (a sealed brick)'}")
    count = convert_takes(brick, folder_map, report, name)
    if count:
        report.say(name, f"converted {count} takes to schema 2 under input/tapes")
    convert_ledger(brick, list(sources), report, name)
    convert_config(brick)

    (brick / "input/evidence.py").unlink(missing_ok=True)
    for relative in BOILERPLATE:
        if relative == "input/tape.py" and not ports:
            continue
        (brick / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATE / relative, brick / relative)
    if sources and not (brick / "input/tapes").is_dir():
        (brick / "input/tapes").mkdir(parents=True)
        (brick / "input/tapes/.gitkeep").write_text("", encoding="utf-8")
    report.say(name, "replaced the boilerplate: input/tape.py, runner/run.py, runner/rng.py")

    if lane == "workflow" and not (brick / "__main__.py").is_file():
        report.note(name, "was a workflow but has no __main__.py: it is an open brick now, and smoke tests need a __main__.py")
    if lane == "pure" and ports:
        report.note(name, "was pure but has ports; it is open now")
    if not ports and lane != "pure":
        report.note(name, "has no ports, so it is a sealed brick now and the sealed rules apply to the whole brick")
    scan(brick, name, report)


def convert(root: Path) -> Report:
    report = Report()
    bricks = root / "bricks"
    if not bricks.is_dir():
        raise SystemExit(f"{bricks}: bricks directory is missing")
    paths = [p for p in sorted(bricks.iterdir()) if p.is_dir() and not p.name.startswith((".", "__"))]
    for brick in paths:
        if not is_old(brick):
            report.say(brick.name, "already in the ports format; skipped")
            continue
        convert_brick(root, brick, report)
    return report


def tree_state(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the project to convert (default: the current directory)")
    parser.add_argument("--dry-run", action="store_true", help="convert a copy and print the report; the project is not touched")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.dry_run:
        before = tree_state(root / "bricks") if (root / "bricks").is_dir() else {}
        with tempfile.TemporaryDirectory() as tmp:
            if (root / "bricks").is_dir():
                shutil.copytree(root / "bricks", Path(tmp) / "bricks", ignore=shutil.ignore_patterns("__pycache__"))
            report = convert(Path(tmp))
            after = tree_state(Path(tmp) / "bricks")
        changed = sorted(
            [f"changed {name}" for name in before.keys() & after.keys() if before[name] != after[name]]
            + [f"added   {name}" for name in after.keys() - before.keys()]
            + [f"removed {name}" for name in before.keys() - after.keys()]
        )
        print("dry run: nothing was written. The conversion would:")
        for line in changed:
            kind, name = line.split(None, 1)
            print(f"  {kind:<8}bricks/{name}")
    else:
        report = convert(root)
    for title, items in (("done", report.done), ("check", report.notes), ("change by hand", report.manual)):
        if items:
            print(f"\n{title}:")
            for item in items:
                print(f"  {item}")
    if not report.manual:
        print("\nno old names are left in the bricks' own code")
    print("\nnext: run python3 tools/lint_bricks.py and your tests; then delete tools/convert_ports.py from the project")
    return 0


if __name__ == "__main__":
    sys.exit(main())
