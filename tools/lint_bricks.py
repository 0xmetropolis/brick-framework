#!/usr/bin/env python3
"""Check the enforceable brick rules using only the Python standard library."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import sys
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from brick_contract import assigned_literal  # type: ignore[no-redef]
else:
    from .brick_contract import assigned_literal


REQUIRED = (
    "__init__.py",
    "contract.py",
    "input/AGENTS.md",
    "input/config.yml",
    "runner/AGENTS.md",
    "runner/__init__.py",
    "runner/rng.py",
    "runner/run.py",
    "runner/ledger",
    "src/AGENTS.md",
)
CONFIG_DEFAULTS = {
    "entries": 10,
    "takes_per_tape": 3,
    "max_take_bytes": 122_880,
    "max_entry_bytes": 122_880,
}
DIRECT_IO = {
    "asyncio", "boto3", "builtins", "ctypes", "fcntl", "fileinput", "ftplib", "glob", "http", "httpx",
    "imaplib", "importlib", "mmap", "multiprocessing", "os", "pathlib", "poplib", "psycopg", "requests",
    "selectors", "shelve", "shutil", "smtplib", "socket", "socketserver", "sqlite3", "ssl", "subprocess",
    "tempfile", "telnetlib", "urllib", "webbrowser", "xmlrpc",
}
# Submodules of an otherwise harmless package that do I/O.
DIRECT_IO_DOTTED = {"logging.handlers", "logging.config"}
# Ways to reach a banned module without importing it by name.
ESCAPE_CALLS = {"__import__", "eval", "exec"}
# The standard-library modules a sealed brick may import: no I/O, clock, randomness, threads or process state.
SEALED_STDLIB = {
    "__future__", "abc", "array", "ast", "base64", "binascii", "bisect", "calendar", "cmath", "codecs",
    "collections", "colorsys", "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal", "difflib",
    "enum", "fnmatch", "fractions", "functools", "graphlib", "hashlib", "heapq", "hmac", "html", "io",
    "ipaddress", "itertools", "json", "keyword", "math", "numbers", "operator", "pprint", "re", "reprlib",
    "shlex", "statistics", "string", "struct", "textwrap", "types", "typing", "unicodedata", "warnings", "zlib",
}
# Calls in tape.py that give an adapter its replay.
REPLAY_CALLS = {"load", "once", "session"}
# Where the source name sits among the arguments of a tape call.
SOURCE_ARGUMENT = {"once": 1, "session": 1, "load": 0, "write": 0}
# A BrickInput field with one of these names selects an operation: the split signal.
DISPATCH_FIELDS = {"op", "flow", "action", "operation", "kind"}
OPTIONAL_FIELD_LIMIT = 6
# Names a run context goes by in src/ and adapters.
CONTEXT_NAMES = {"ctx", "context", "run_context"}
# An environment variable that switches behavior is a mode the run does not record.
ENV_SWITCH = re.compile(r"(?i)replay|fresh|record|mode|live|offline")
IN_MEMORY = {"BytesIO", "StringIO"}
PORT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
SOURCE_FIELDS = {"kind", "epoch", "label", "followable"}
JOINT_FIELDS = {"kind", "policy", "pin", "follow"}
# Declarations that PORTS replaced; a manifest that still has one is in the old format.
OLD_DECLARATIONS = ("SIBLING_DEPENDENCIES", "LIVE_ADAPTER", "MODE_PASSING", "LANE")
CONVERTER = "tools/convert_ports.py"
LOCAL_SUFFIX = ".local.json"
SENSITIVE = {
    "access_token", "api_key", "authorization", "client_secret", "cookie", "cookies",
    "headers", "password", "refresh_token", "secret", "set-cookie", "token", "x-api-key",
}


@dataclass
class Contract:
    version: int = 0
    ports: dict[str, dict[str, Any]] = field(default_factory=dict)
    owned_state: tuple[str, ...] = ()
    deterministic: bool | None = None
    declared: bool = True

    @property
    def sealed(self) -> bool:
        """No ports: the brick reaches nothing outside itself."""
        return self.declared and not self.ports

    @property
    def joints(self) -> dict[str, dict[str, Any]]:
        return {name: port for name, port in self.ports.items() if port.get("kind") == "joint"}

    @property
    def sources(self) -> dict[str, dict[str, Any]]:
        return {name: port for name, port in self.ports.items() if port.get("kind") == "source"}


class Lint:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def add(self, path: Path, message: str, *, warning: bool = False) -> None:
        try:
            name = path.relative_to(self.root)
        except ValueError:
            name = path
        target = self.warnings if warning else self.errors
        target.append(f"{name}: {message}")

    def tree(self, path: Path) -> ast.Module | None:
        if not path.is_file():
            return None
        try:
            return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            self.add(path, f"cannot parse Python: {exc}")
            return None


def config_number(text: str, key: str, lint: Lint, path: Path) -> int:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(\d+)\s*(?:#.*)?$", text)
    if not match or int(match.group(1)) < 1:
        lint.add(path, f"missing positive integer setting {key!r}")
        return CONFIG_DEFAULTS[key]
    return int(match.group(1))


def imported_modules(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return ["." * node.level + (node.module or "")]


def stdlib_names() -> frozenset[str] | None:
    """The standard library's top-level module names (Python 3.10+), or None where unknown."""
    return getattr(sys, "stdlib_module_names", None)


def escape_hatches(path: Path, tree: ast.Module, lint: Lint, label: str) -> None:
    """Dynamic import, eval/exec and ``sys.modules`` reach banned modules without naming them."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and called_name(node) in ESCAPE_CALLS and isinstance(node.func, ast.Name):
            lint.add(path, f"{label} may not call {called_name(node)}(); it reaches modules the import rules cannot see")
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "modules"
            and isinstance(node.value, ast.Name)
            and node.value.id == "sys"
        ):
            lint.add(path, f"{label} may not use sys.modules; it reaches modules the import rules cannot see")
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            lint.add(path, f"{label} may not use __builtins__; it reaches open() and __import__ unseen")


def plan_override(node: ast.AST) -> bool:
    """True for ``dict(ctx, plan=...)``: src building a context with another plan."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in CONTEXT_NAMES
        and any(keyword.arg == "plan" for keyword in node.keywords)
    )


def env_switch(node: ast.AST) -> str:
    """The variable name when ``node`` reads a mode-like environment variable, else ''."""

    def is_environ(value: ast.AST) -> bool:
        return isinstance(value, ast.Attribute) and value.attr == "environ"

    key = None
    if isinstance(node, ast.Subscript) and is_environ(node.value):
        key = node.slice
    elif isinstance(node, ast.Call) and node.args:
        if isinstance(node.func, ast.Attribute) and (
            (node.func.attr == "get" and is_environ(node.func.value)) or node.func.attr == "getenv"
        ):
            key = node.args[0]
    if isinstance(key, ast.Constant) and isinstance(key.value, str) and ENV_SWITCH.search(key.value):
        return key.value
    return ""


def opens_a_file(node: ast.Call) -> bool:
    """A call named ``open``, except ``x.open(BytesIO(...))``, which cannot reach a file."""
    if called_name(node) != "open":
        return False
    in_memory = (
        isinstance(node.func, ast.Attribute)
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Call)
        and called_name(node.args[0]) in IN_MEMORY
    )
    return not in_memory


def called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def annotation_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def plan_read(node: ast.AST) -> bool:
    """True when ``node`` reads ``plan`` from a run context: ``ctx["plan"]`` or ``ctx.get("plan")``."""
    if isinstance(node, ast.Subscript):
        target, key = node.value, node.slice
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
        target, key = node.func.value, node.args[0]
    else:
        return False
    return (
        isinstance(target, ast.Name)
        and target.id in CONTEXT_NAMES
        and isinstance(key, ast.Constant)
        and key.value == "plan"
    )


def lint_contract(brick: Path, lint: Lint) -> Contract:
    path = brick / "contract.py"
    tree = lint.tree(path)
    if not tree:
        return Contract()

    version = assigned_literal(tree, "CONTRACT_VERSION")
    owned_state = assigned_literal(tree, "OWNED_STATE")
    deterministic = assigned_literal(tree, "DETERMINISTIC")
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        lint.add(path, "CONTRACT_VERSION must be a positive integer")
        version = 0
    old = [name for name in OLD_DECLARATIONS if any(
        isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(isinstance(t, ast.Name) and t.id == name for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
        for node in tree.body
    )]
    if old:
        lint.add(
            path,
            f"{', '.join(old)} belong to the old manifest format: declare PORTS instead, and the shape "
            f"(sealed, entry or open) is computed (run python3 {CONVERTER} once to convert a project)",
        )
    ports = lint_ports(path, tree, lint)
    declared = ports is not None
    if not isinstance(owned_state, (tuple, list)) or not all(
        isinstance(item, str) and item for item in owned_state
    ):
        lint.add(path, "OWNED_STATE must contain non-empty resource identifiers")
        owned_state = ()
    if deterministic is not None and not isinstance(deterministic, bool):
        lint.add(path, "DETERMINISTIC must be True or False")
        deterministic = None
    for class_name in ("BrickInput", "BrickOutput"):
        declaration = classes.get(class_name)
        if not declaration or not any(annotation_name(base) == "TypedDict" for base in declaration.bases):
            lint.add(path, f"{class_name} must be declared as a TypedDict")
    declaration = classes.get("BrickInput")
    if declaration:
        fields = [m.target.id for m in declaration.body if isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name)]
        for field_name in fields:
            if field_name in DISPATCH_FIELDS:
                lint.add(
                    path,
                    f"BrickInput.{field_name} selects an operation; one run is one operation, so split the "
                    "brick (bricks/AGENTS.md: split when run becomes a dispatcher)",
                    warning=True,
                )
        optional = any(
            keyword.arg == "total" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
            for keyword in declaration.keywords
        )
        if optional and len(fields) > OPTIONAL_FIELD_LIMIT:
            lint.add(
                path,
                f"BrickInput has {len(fields)} fields and all are optional, so the type promises almost nothing; "
                "make the required fields required, or split the brick",
                warning=True,
            )
    for class_name in ("BrickInput", "BrickOutput"):
        declaration = classes.get(class_name)
        for member in declaration.body if declaration else ():
            if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name) and any(
                isinstance(part, ast.Name) and part.id in {"bytes", "bytearray"} for part in ast.walk(member.annotation)
            ):
                lint.add(
                    path,
                    f"{class_name}.{member.target.id} holds bytes, but inputs and outputs are recorded as JSON; "
                    "use a base64 string or a manifest (input/AGENTS.md, Recorded data)",
                    warning=True,
                )
    return Contract(version, ports or {}, tuple(owned_state), deterministic, declared)


def json_safe(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def lint_ports(path: Path, tree: ast.Module, lint: Lint) -> dict[str, dict[str, Any]] | None:
    """Validate ``PORTS`` and return the well-formed ports, or None when they are not declared as a dict."""
    declared = assigned_literal(tree, "PORTS")
    if not any(
        (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PORTS" for t in node.targets))
        or (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PORTS")
        for node in tree.body
    ):
        lint.add(path, "PORTS is not declared; declare every source and joint (PORTS = {} for a brick with none)")
        return None
    if not isinstance(declared, dict):
        lint.add(path, "PORTS must be a dict literal mapping port names to declarations")
        return None
    ports: dict[str, dict[str, Any]] = {}
    for name, port in declared.items():
        if not isinstance(name, str) or not PORT_NAME.fullmatch(name):
            lint.add(path, f"port name {name!r} must be lowercase letters, digits and underscores")
            continue
        if not isinstance(port, dict) or port.get("kind") not in {"source", "joint"}:
            lint.add(path, f"port {name!r} needs kind 'source' or 'joint'")
            continue
        kind = port["kind"]
        allowed = SOURCE_FIELDS if kind == "source" else JOINT_FIELDS
        for key in sorted(set(port) - allowed, key=str):
            lint.add(path, f"port {name!r} has field {key!r}, which a {kind} port does not take")
        if kind == "source":
            epoch = port.get("epoch")
            if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
                lint.add(path, f"source {name!r} needs epoch: a positive integer, bumped when the adapter's request or response meaning changes")
            label = port.get("label")
            if "label" in port and (
                not isinstance(label, dict)
                or not label
                or not all(isinstance(key, str) for key in label)
                or not json_safe(label)
            ):
                lint.add(path, f"source {name!r} label must be a non-empty dict of what the source is (a version, a build, a protocol)")
            if "followable" in port and not isinstance(port["followable"], bool):
                lint.add(path, f"source {name!r} followable must be True or False")
        else:
            if port.get("policy") not in {"eventual", "orchestrated"}:
                lint.add(path, f"joint {name!r} needs policy 'eventual' or 'orchestrated'")
            pin = port.get("pin")
            if not isinstance(pin, int) or isinstance(pin, bool) or pin < 1:
                lint.add(path, f"joint {name!r} needs pin: the integer CONTRACT_VERSION of {name!r} it was written against")
            follows = port.get("follow")
            if "follow" in port:
                if not isinstance(follows, (tuple, list)) or not follows or not all(isinstance(item, str) and item for item in follows):
                    lint.add(path, f"joint {name!r} follow must list the sibling's followable source names")
                elif port.get("policy") != "orchestrated":
                    lint.add(path, f"joint {name!r} follows sources, so its policy must be 'orchestrated'")
        ports[name] = port
    return ports


def lint_python(brick: Path, contract: Contract, lint: Lint) -> None:
    entry = lint.tree(brick / "__init__.py")
    if entry:
        imports_run = any(
            isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module == "runner.run"
            and [name.name for name in node.names] == ["run"]
            for node in entry.body
        )
        exports = None
        for node in entry.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                try:
                    exports = ast.literal_eval(node.value)
                except (TypeError, ValueError):
                    pass
        if not imports_run or exports != ["run"]:
            lint.add(brick / "__init__.py", "must import and export only .runner.run")

    runner = lint.tree(brick / "runner/run.py")
    if runner:
        function = next(
            (node for node in runner.body if isinstance(node, ast.FunctionDef) and node.name == "run"),
            None,
        )
        options = {arg.arg for arg in function.args.kwonlyargs} if function else set()
        if not {"fresh", "record"}.issubset(options):
            lint.add(brick / "runner/run.py", "run needs keyword-only fresh and record options")
        if function:
            input_type = annotation_name(function.args.args[0].annotation) if function.args.args else ""
            if input_type != "BrickInput" or annotation_name(function.returns) != "BrickOutput":
                lint.add(brick / "runner/run.py", "run must use BrickInput and BrickOutput annotations")

    actual_dependencies: set[str] = set()
    for path in brick.rglob("*.py"):
        tree = lint.tree(path)
        if not tree:
            continue
        relative = path.relative_to(brick).parts
        role = relative[0] if len(relative) > 1 else "entry"
        adapter = relative[:2] == ("input", "adapters")
        calls: set[str] = set()
        imports_sibling = False
        sibling_runs: dict[str, str] = {}
        follow_flags: dict[str, str] = {}
        if adapter:
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bricks.") and node.level == 0:
                    for alias in node.names:
                        if alias.name == "run":
                            sibling_runs[alias.asname or alias.name] = node.module.split(".")[1]
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    named = follow_joint(node.value)
                    if named:
                        follow_flags[node.targets[0].id] = named
            if len(relative) == 3 and path.name != "__init__.py" and path.stem not in contract.ports:
                lint.add(path, f"adapter {path.name} has no port {path.stem!r} in PORTS; one adapter file implements one declared port")
        if role == "src":
            escape_hatches(path, tree, lint, "src")
        elif role != "runner" and any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__"
            for node in ast.walk(tree)
        ):
            lint.add(path, "dynamic imports hide brick boundaries")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                calls.add(called_name(node))
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom):
                    package_depth = len(relative[:-1])
                    if node.level > package_depth + 1:
                        lint.add(path, "relative import may not escape the brick")
                for module in imported_modules(node):
                    parts = module.lstrip(".").split(".")
                    if role == "runner" and "input" in parts:
                        lint.add(path, "runner may not import input")
                    if adapter and ({"src", "runner"} & set(parts)):
                        lint.add(path, "adapter may not import src or runner")
                    if role == "src":
                        if "runner" in parts:
                            lint.add(path, "src may not import runner")
                        if parts[0] in DIRECT_IO:
                            lint.add(path, f"src imports direct-I/O module {parts[0]!r}")
                        elif ".".join(parts[:2]) in DIRECT_IO_DOTTED:
                            lint.add(path, f"src imports direct-I/O module {'.'.join(parts[:2])!r}")
                    elif role != "runner" and parts[0] == "importlib":
                        lint.add(path, "dynamic imports hide brick boundaries")
                    if len(parts) > 1 and parts[0] == "bricks" and parts[1] != brick.name:
                        actual_dependencies.add(parts[1])
                        imports_sibling = True
                        if not adapter:
                            lint.add(path, "only an input adapter may import a sibling brick")
                        elif (
                            not isinstance(node, ast.ImportFrom)
                            or module != f"bricks.{parts[1]}"
                            or [n.name for n in node.names] != ["run"]
                        ):
                            lint.add(path, "sibling adapter may import only run")
                        if parts[1] not in contract.joints:
                            lint.add(path, f"joint {parts[1]!r} is not declared in PORTS")
                        elif adapter and path.stem != parts[1]:
                            lint.add(path, f"the adapter for joint {parts[1]!r} must be named {parts[1]}.py")
            if role == "src" and plan_read(node):
                lint.add(
                    path,
                    "src reads the run plan; each adapter owns replay, fresh and record behavior, "
                    "so label recorded data by what the adapter returned (input/AGENTS.md, Recorded data)",
                    warning=True,
                )
            if role == "src" and plan_override(node):
                lint.add(
                    path,
                    "src overrides the run plan; the plan belongs to the run and its adapters. Ask for a "
                    "source fresh or recorded by naming its port in run(..., fresh=...) "
                    "(input/AGENTS.md, Recorded data)",
                    warning=True,
                )
            if adapter and env_switch(node):
                lint.add(
                    path,
                    f"adapter switches behavior on environment variable {env_switch(node)!r}; that is a mode "
                    "no ledger entry shows. Ask for the source fresh by naming its port in run(..., fresh=...), or "
                    "pass the choice in the brick input",
                    warning=True,
                )
            if role == "src" and isinstance(node, ast.Call) and opens_a_file(node):
                lint.add(
                    path,
                    "src filesystem access must use an adapter (in memory is fine: "
                    "x.open(io.BytesIO(data)) is allowed when the buffer is built in the call)",
                )
            if adapter and isinstance(node, ast.Call):
                lint_tape_call(path, node, contract, lint)
                lint_follow_call(path, node, contract, sibling_runs, follow_flags, lint)
        if adapter and not imports_sibling and not calls & REPLAY_CALLS:
            lint.add(
                path,
                f"adapter for source {path.stem!r} never plays a tape (tape.once or tape.session), so it has no replay; "
                "a file or tool adapter replays a recorded manifest (input/AGENTS.md, Recorded data)",
                warning=True,
            )

    for name in sorted(contract.ports):
        if not (brick / "input/adapters" / f"{name}.py").is_file():
            lint.add(brick / "contract.py", f"port {name!r} has no adapter file input/adapters/{name}.py")
    for joint in contract.joints.keys() - actual_dependencies:
        lint.add(brick / "contract.py", f"declared joint {joint!r} has no static adapter import", warning=True)


def read_json(path: Path, lint: Lint) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        lint.add(path, f"invalid JSON: {exc}")
        return None


def check_secrets(path: Path, value: Any, lint: Lint) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in SENSITIVE and item != "<redacted>":
                lint.add(path, f"sensitive field {key!r} is not redacted")
            check_secrets(path, item, lint)
    elif isinstance(value, list):
        for item in value:
            check_secrets(path, item, lint)


def label_differences(recorded: Any, wanted: Any) -> list[str]:
    """Which keys of a take's label differ from the port's, as ``key: recorded X, port Y``."""
    recorded = recorded if isinstance(recorded, dict) else {}
    wanted = wanted if isinstance(wanted, dict) else {}
    return [
        f"{key}: recorded {json.dumps(recorded.get(key, 'nothing'))}, port {json.dumps(wanted.get(key, 'nothing'))}"
        for key in sorted(recorded.keys() | wanted.keys())
        if recorded.get(key, "\0") != wanted.get(key, "\0")
    ]


def lint_records(brick: Path, contract: Contract, limits: dict[str, int], lint: Lint) -> None:
    for old, advice in (
        ("input/data", "takes now live in input/tapes"),
        ("input/evidence.py", "the loader is now input/tape.py"),
        ("runner/runs", "run records are now ledger entries in runner/ledger"),
    ):
        if (brick / old).exists():
            lint.add(brick / old, f"old path of the previous format; run python3 {CONVERTER} once ({advice})")
    tapes = brick / "input/tapes"
    counts: dict[str, int] = {}
    for path in sorted(tapes.rglob("*.json")):
        relative = path.relative_to(tapes)
        if len(relative.parts) != 2:
            lint.add(path, "a take path must be <source>/<take>.json")
            continue
        source, filename = relative.parts
        take = Path(filename).stem
        counts[source] = counts.get(source, 0) + 1
        port = contract.sources.get(source)
        if port is None:
            lint.add(path, f"tape folder {source!r} has no source port in PORTS; declare the port or delete the take")
        size = path.stat().st_size
        if size > limits["max_take_bytes"]:
            lint.add(path, "take exceeds max_take_bytes; store a projection or split the session into more takes")
        elif size * 5 > limits["max_take_bytes"] * 4:
            lint.add(
                path,
                f"take is {size * 100 // limits['max_take_bytes']}% of max_take_bytes; "
                "store a projection of the response or split a session into takes per window "
                "before the next re-record fails",
                warning=True,
            )
        record = read_json(path, lint)
        if not isinstance(record, dict):
            continue
        required = {"schema_version", "source", "take", "epoch", "recorded_in", "request"}
        if record.get("schema_version") != 2:
            lint.add(path, f"take is not in schema 2 (the old format?); run python3 {CONVERTER} once to convert the project")
            continue
        if required - record.keys():
            lint.add(path, "take is missing required fields")
        if record.get("source") != source or record.get("take") != take:
            lint.add(path, "source and take must match the path")
        if ("response" in record) == ("error" in record):
            lint.add(path, "a take needs exactly one of response or error")
        canonical = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
        if path.read_bytes() != canonical:
            lint.add(path, "take JSON is not canonical")
        check_secrets(path, record, lint)
        if port is not None and isinstance(port.get("epoch"), int) and record.get("epoch") != port["epoch"]:
            lint.add(
                path,
                f"take recorded at epoch {record.get('epoch')}, port {source!r} is at epoch {port['epoch']}; re-record it",
            )
        if port is not None:
            request = record.get("request")
            recorded_label = request.get("label") if isinstance(request, dict) else None
            differences = label_differences(recorded_label, port.get("label"))
            if differences:
                lint.add(
                    path,
                    f"take label differs from port {source!r} ({'; '.join(differences)}); re-record it",
                )
    for source, count in counts.items():
        if count > limits["takes_per_tape"]:
            lint.add(
                tapes / source,
                "too many takes; a take per window or per instance is fine, but raise "
                "retention.takes_per_tape in config.yml deliberately and say why",
            )

    entries = sorted((brick / "runner/ledger").glob("*.json"))
    # Local entries (replay-only runs and test runs) are untracked and bounded on their own.
    tracked = [path for path in entries if not path.name.endswith(LOCAL_SUFFIX)]
    if len(tracked) > limits["entries"]:
        lint.add(brick / "runner/ledger", "too many retained ledger entries")
    for path in entries:
        if path.stat().st_size > limits["max_entry_bytes"]:
            lint.add(path, "ledger entry exceeds max_entry_bytes")
        entry = read_json(path, lint)
        if (
            path in tracked
            and isinstance(entry, dict)
            and not (entry.get("fresh") or entry.get("followed") or "error" in entry)
        ):
            lint.add(
                path,
                "a tracked ledger entry is for a run that went fresh, recorded or failed; "
                "a replay-only run writes a local .local.json entry",
            )


def lint_smokes(brick: Path, contract: Contract, lint: Lint) -> None:
    tests_dir = brick / "runner/tests"
    tests = sorted(tests_dir.glob("test_*.py")) if tests_dir.exists() else []
    if not tests:
        return
    if not (tests_dir / "__init__.py").is_file():
        lint.add(tests_dir / "__init__.py", "smoke-test package marker is missing")
    if not (brick / "__main__.py").is_file():
        lint.add(tests_dir, "smoke tests belong only to entry bricks (a brick with a __main__.py)")
    if len(tests) > 3:
        lint.add(tests_dir, "an entry brick may have at most three smoke-test files")
    for path in tests:
        tree = lint.tree(path)
        if not tree:
            continue
        if any(
            isinstance(node, ast.Attribute) and node.attr.startswith("skip")
            for node in ast.walk(tree)
        ):
            lint.add(path, "contains a skipped smoke test; it proves no integration", warning=True)
        if "BRICK_TEST" not in path.read_text(encoding="utf-8"):
            lint.add(
                path,
                "smoke test does not set BRICK_TEST=1, so a fresh or failing run lands in tracked ledger history; "
                "set it in setUp with os.environ and restore it in a cleanup",
                warning=True,
            )
        calls_run = any(
            isinstance(node, ast.Call) and called_name(node) == "run" for node in ast.walk(tree)
        )
        if not calls_run:
            lint.add(path, "smoke test must call the brick's public run")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                lint.add(path, "smoke test must import run from the top-level brick")
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("bricks"):
                if node.module != f"bricks.{brick.name}" or [item.name for item in node.names] != ["run"]:
                    lint.add(path, "smoke test may import only its brick's top-level run")
            if isinstance(node, ast.Import) and any(item.name.startswith("bricks") for item in node.names):
                lint.add(path, "smoke test must use from bricks.<name> import run")


SEALED_FORBIDDEN = DIRECT_IO | {"random", "time"}
CLOCK_CALLS = {"now", "today", "utcnow"}
# What a brick that declares DETERMINISTIC = True keeps out of src/.
NONDETERMINISTIC_MODULES = {
    "concurrent", "multiprocessing", "random", "secrets", "threading", "time", "uuid",
}


def lint_deterministic(brick: Path, contract: Contract, lint: Lint) -> None:
    """``DETERMINISTIC = True`` is a promise ``src/`` can be held to; ``False`` says it is not made.

    A brick that says nothing makes no promise and gets no check. A brick that
    declares ``True`` may not reach for the clock, randomness, threads or
    generated IDs in ``src/`` (adapters are where such things enter, as inputs).
    A sealed brick is held to this across the whole brick, so it may not say ``False``.
    """
    if contract.sealed and contract.deterministic is False:
        lint.add(brick / "contract.py", "a sealed brick is deterministic by definition; remove DETERMINISTIC = False")
    if contract.deterministic is not True or contract.sealed:
        return
    for path in sorted((brick / "src").rglob("*.py")):
        tree = lint.tree(path)
        if not tree:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in imported_modules(node):
                    name = module.lstrip(".").split(".")[0]
                    if name in NONDETERMINISTIC_MODULES:
                        lint.add(
                            path,
                            f"DETERMINISTIC = True, but src imports {name!r}; take it as input through an "
                            "adapter, or declare DETERMINISTIC = False and say why",
                        )
            if isinstance(node, ast.Call) and called_name(node) in CLOCK_CALLS:
                lint.add(
                    path,
                    f"DETERMINISTIC = True, but src reads the clock with {called_name(node)}(); "
                    "take the time as input, or declare DETERMINISTIC = False and say why",
                )


def lint_sealed(brick: Path, contract: Contract, lint: Lint) -> None:
    """A sealed brick has no ports, so its output is a function of its input and nothing else.

    Ports are the only sanctioned channel to the outside, so a sealed brick has
    no adapters and no tapes. The direct-I/O
    ban every ``src/`` already carries widens to the whole brick, joined by
    ``random`` and ``time``, and by the clock calls the ``src/`` contract has
    always forbidden in prose. ``runner/`` is the one exemption: it writes
    ledger entries and owns ``rng.py``, so it keeps the filesystem and
    randomness the rest of the brick gives up. ``datetime`` stays importable:
    arithmetic on a caller-supplied time is allowed; reading ``now()`` is not.
    """
    for folder, what in (("input/adapters", "adapters"), ("input/tapes", "tapes")):
        for path in sorted((brick / folder).rglob("*")) if (brick / folder).is_dir() else ():
            if path.is_file() and path.name != ".gitkeep":
                lint.add(path, f"sealed brick may not have {what}: it has no ports")
    stdlib = stdlib_names()
    for path in brick.rglob("*.py"):
        relative = path.relative_to(brick).parts
        if relative[0] == "runner":
            continue
        tree = lint.tree(path)
        if not tree:
            continue
        if relative[0] != "src":
            escape_hatches(path, tree, lint, "sealed brick")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in imported_modules(node):
                    name = module.lstrip(".").split(".")[0]
                    if (
                        stdlib is not None
                        and name in stdlib
                        and name not in SEALED_STDLIB
                        and name not in SEALED_FORBIDDEN
                    ):
                        lint.add(path, f"sealed brick imports standard-library module {name!r}, which is not on the sealed allowlist")
                    if ".".join(module.lstrip(".").split(".")[:2]) in DIRECT_IO_DOTTED and relative[0] != "src":
                        lint.add(path, f"sealed brick imports {'.'.join(module.split('.')[:2])!r} outside runner/")
                    # src/ already reports DIRECT_IO under the rule every brick gets.
                    if name in SEALED_FORBIDDEN and not (relative[0] == "src" and name in DIRECT_IO):
                        hint = (
                            "; a sealed brick has no ports, so delete input/tape.py"
                            if relative == ("input", "tape.py")
                            else ""
                        )
                        lint.add(path, f"sealed brick imports {name!r} outside runner/{hint}")
            if isinstance(node, ast.Call) and called_name(node) in CLOCK_CALLS:
                lint.add(path, f"sealed brick reads the clock with {called_name(node)}()")


def lint_entry(brick: Path, contract: Contract, lint: Lint) -> None:
    """An entry brick is invoked as a whole operation, never imported for its parts.

    Two consequences, one enforced here and one in the graph. Here: its
    ``__main__.py`` may import nothing from the brick tree except the brick's
    own ``run`` -- the same rule a joint adapter lives under, applied to the
    outside world. In the graph: no joint may point at an entry brick. Smoke
    tests, which prove integration through ``run``, belong only here.
    """
    main = lint.tree(brick / "__main__.py")
    if not main:
        return
    own_run = {".", ".runner.run", f"bricks.{brick.name}"}
    for node in ast.walk(main):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for module in imported_modules(node):
            inside = module.startswith(".") or module.split(".")[0] == "bricks"
            if not inside:
                top = module.split(".")[0]
                if top in DIRECT_IO - {"os", "pathlib"}:
                    lint.add(
                        brick / "__main__.py",
                        f"__main__ imports direct-I/O module {top!r}; it parses arguments, calls run and "
                        "prints or writes what run returned; effects belong in an adapter",
                        warning=True,
                    )
                continue
            names = [item.name for item in node.names]
            if not (isinstance(node, ast.ImportFrom) and module in own_run and names == ["run"]):
                lint.add(brick / "__main__.py", "__main__ may import only this brick's run")


def lint_brick(brick: Path, contract: Contract, lint: Lint) -> None:
    required = list(REQUIRED)
    if contract.ports:
        required.append("input/adapters")
    if contract.sources:
        required.append("input/tapes")
    for relative in required:
        if not (brick / relative).exists():
            lint.add(brick / relative, "required brick path is missing")
    config_path = brick / "input/config.yml"
    config = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    limits = {key: config_number(config, key, lint, config_path) for key in CONFIG_DEFAULTS}
    lint_python(brick, contract, lint)
    lint_records(brick, contract, limits, lint)
    lint_smokes(brick, contract, lint)
    lint_adapter_functions(brick, lint)
    lint_deterministic(brick, contract, lint)
    if contract.sealed:
        lint_sealed(brick, contract, lint)
    if (brick / "__main__.py").is_file():
        lint_entry(brick, contract, lint)


COPY_MARKER = re.compile(r"^#\s*BOILERPLATE:\s*([a-z0-9_.-]+)@(\d+)\s*$")


def lint_copies(bricks: list[Path], lint: Lint) -> None:
    """Warn when copies of one boilerplate file have drifted apart.

    Some files are copied into every brick on purpose (``input/tape.py``,
    the runner files). A copy carries ``# BOILERPLATE: <id>@<n>`` in its first
    lines. Copies with the same id and version must be byte-identical; copies
    with different versions say which brick is behind. A deliberate fork bumps
    ``@n`` or takes a new id, and the warning then names it instead of hiding it.
    """
    copies: dict[str, list[tuple[Path, int, bytes]]] = {}
    for brick in bricks:
        for path in sorted(brick.rglob("*.py")):
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for line in data.decode("utf-8", errors="replace").splitlines()[:5]:
                match = COPY_MARKER.match(line.strip())
                if match:
                    copies.setdefault(match.group(1), []).append((path, int(match.group(2)), data))
                    break
    for name, found in sorted(copies.items()):
        first_path, first_version, first_data = found[0]
        for path, version, data in found[1:]:
            if version != first_version:
                lint.add(
                    path,
                    f"copy of {name!r} is at @{version} but {first_path.relative_to(lint.root)} is at "
                    f"@{first_version}; bring the older copy up to date",
                    warning=True,
                )
            elif data != first_data:
                lint.add(
                    path,
                    f"copy of {name!r}@{version} differs from {first_path.relative_to(lint.root)}; "
                    "sync the copies, or bump @n here to record a deliberate fork",
                    warning=True,
                )


def tape_source(node: ast.Call) -> str:
    """The source port a tape call names with a literal, else ''."""
    position = SOURCE_ARGUMENT.get(called_name(node))
    if position is None or len(node.args) <= position:
        return ""
    argument = node.args[position]
    return argument.value if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else ""


def lint_tape_call(path: Path, node: ast.Call, contract: Contract, lint: Lint) -> None:
    """One adapter file is one source port: its tape calls name that port, and a session needs a label."""
    stem = path.stem
    named = tape_source(node)
    if named and stem in contract.sources and named != stem:
        lint.add(
            path,
            f"adapter {path.name} uses the tape of source {named!r}; one adapter file implements one port, so use {stem!r}",
        )
    if called_name(node) == "session" and stem in contract.sources and not contract.sources[stem].get("label"):
        lint.add(
            path,
            f"adapter {path.name} opens a session but source {stem!r} has no label in PORTS; name what the source is "
            "(a game version, a build, a protocol) so takes recorded against another version are found stale",
            warning=True,
        )


def follow_joint(node: ast.AST) -> str:
    """The joint name when ``node`` is ``follow(<context>, "<joint>")``, else ''."""
    if (
        isinstance(node, ast.Call)
        and called_name(node) == "follow"
        and len(node.args) == 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ):
        return node.args[1].value
    return ""


def passes_flags_from_helper(value: ast.AST, key: str, joint: str, flags: dict[str, str]) -> bool:
    """True for ``follow(ctx, "<joint>")["<key>"]`` or ``<name>["<key>"]`` where name came from that call."""
    if not (isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant) and value.slice.value == key):
        return False
    if follow_joint(value.value) == joint:
        return True
    return isinstance(value.value, ast.Name) and flags.get(value.value.id) == joint


def lint_follow_call(
    path: Path,
    node: ast.Call,
    contract: Contract,
    sibling_runs: dict[str, str],
    flags: dict[str, str],
    lint: Lint,
) -> None:
    """Following: only a declared, helper-fed call may give a sibling fresh or record."""
    if called_name(node) == "follow" and len(node.args) == 2 and isinstance(node.args[1], ast.Constant):
        joint = node.args[1].value
        if not contract.joints.get(joint, {}).get("follow"):
            lint.add(path, f"follow for {joint!r}, which has no follow list in PORTS (contract.py)")
        return
    target = sibling_runs.get(node.func.id) if isinstance(node.func, ast.Name) else None
    if target is None and called_name(node) != "run":
        return
    forwarded = {word.arg for word in node.keywords} & {"fresh", "record"}
    spread = any(word.arg is None for word in node.keywords)
    if target is not None and contract.joints.get(target, {}).get("follow"):
        if spread:
            lint.add(path, "a sibling run that is followed may not receive expanded keyword arguments")
        if forwarded != {"fresh", "record"}:
            lint.add(path, f"following {target!r} must give both fresh and record, from follow(ctx, {target!r})")
        for word in node.keywords:
            if word.arg in {"fresh", "record"} and not passes_flags_from_helper(word.value, word.arg, target, flags):
                lint.add(
                    path,
                    f"{word.arg}= for {target!r} must be follow(ctx, {target!r})[{word.arg!r}], "
                    "not a value written here",
                )
        return
    if forwarded or {word.arg for word in node.keywords} & {"save"}:
        lint.add(
            path,
            "a sibling run may not receive fresh or record unless its joint lists follow in PORTS "
            "and the flags come from follow(ctx, <joint>) (README: Source ports)",
        )


def lint_adapter_functions(brick: Path, lint: Lint) -> None:
    """Warn on an adapter function that does I/O, takes no run context, and is called from outside its file.

    Such a function cannot see the plan, so it behaves the same in replay,
    fresh and record: a replayed run reads the real disk or network. Helpers used
    only inside their own adapter are not flagged.
    """
    adapters = sorted((brick / "input/adapters").glob("*.py"))
    candidates: dict[str, dict[str, tuple[Path, ast.FunctionDef]]] = {}
    for path in adapters:
        tree = lint.tree(path)
        if not tree:
            continue
        io_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                io_names |= {(a.asname or a.name).split(".")[0] for a in node.names if a.name.split(".")[0] in DIRECT_IO}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and node.module.split(".")[0] in DIRECT_IO:
                io_names |= {a.asname or a.name for a in node.names}
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            params = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            if params & CONTEXT_NAMES:
                continue
            does_io = any(
                (isinstance(part, ast.Name) and part.id in io_names)
                or (isinstance(part, ast.Call) and called_name(part) == "open")
                for part in ast.walk(node)
            )
            if does_io:
                candidates.setdefault(path.stem, {})[node.name] = (path, node)
    if not candidates:
        return
    used: dict[str, set[str]] = {stem: set() for stem in candidates}
    for path in brick.rglob("*.py"):
        if path.parent == brick / "input/adapters" and path.stem in candidates:
            continue
        tree = lint.tree(path)
        if not tree:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in used:
                used[node.value.id].add(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[-1] in used:
                used[node.module.split(".")[-1]] |= {a.name for a in node.names}
    for stem, functions in candidates.items():
        for name, (path, node) in functions.items():
            if name in used[stem]:
                lint.add(
                    path,
                    f"{name}() does I/O and takes no run context, so it behaves the same in every mode; "
                    "take ctx and answer from a take in replay",
                    warning=True,
                )


def lint_graph(
    bricks: list[Path], contracts: dict[str, Contract], lint: Lint
) -> None:
    names = set(contracts)
    owners: dict[str, str] = {}
    graph: dict[str, set[str]] = {name: set() for name in names}

    def where(name: str) -> Path:
        return next(brick for brick in bricks if brick.name == name) / "contract.py"

    for name, contract in contracts.items():
        for dependency, port in contract.joints.items():
            if dependency == name:
                lint.add(where(name), "brick may not depend on itself")
            elif dependency not in names:
                lint.add(where(name), f"joint {dependency!r} names a brick that does not exist")
            else:
                if (where(dependency).parent / "__main__.py").is_file():
                    lint.add(
                        where(name),
                        f"joint {dependency!r} is an entry brick; an entry brick is invoked as a whole, not depended on",
                    )
                pin, version = port.get("pin"), contracts[dependency].version
                if isinstance(pin, int) and version and pin != version:
                    lint.add(
                        where(name),
                        f"joint {dependency!r} pins version {pin}, but {dependency!r} is at version {version}; "
                        "update the adapter and the pin together",
                    )
                for source in port.get("follow") or ():
                    if not contracts[dependency].sources.get(source, {}).get("followable"):
                        lint.add(
                            where(name),
                            f"joint {dependency!r} follows {source!r}, which is not a followable source of {dependency!r}",
                        )
                graph[name].add(dependency)
        for resource in contract.owned_state:
            if resource in owners:
                lint.add(
                    next(brick for brick in bricks if brick.name == name) / "contract.py",
                    f"state {resource!r} is already owned by {owners[resource]!r}",
                )
            else:
                owners[resource] = name

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if name in visiting:
            cycle = " -> ".join((*trail, name))
            lint.add(lint.root / "bricks", f"joint cycle: {cycle}")
            return
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency, (*trail, name))
        visiting.remove(name)
        visited.add(name)

    for name in sorted(names):
        visit(name, ())


def lint_repo(root: Path) -> Lint:
    """Lint every brick under ``root`` and return the findings without printing.

    ``main`` is the command; this is the function a test calls on a fixture tree.
    """
    lint = Lint(root)
    bricks = root / "bricks"
    for path in (root / "AGENTS.md", bricks / "AGENTS.md"):
        if not path.is_file():
            lint.add(path, "required inherited contract is missing")
    for path in root.rglob("BRICK.md"):
        lint.add(path, "contract must be named AGENTS.md")
    if bricks.is_dir():
        brick_paths = sorted(
            path
            for path in bricks.iterdir()
            if path.is_dir() and not path.name.startswith((".", "__"))
        )
        contracts = {brick.name: lint_contract(brick, lint) for brick in brick_paths}
        lint_graph(brick_paths, contracts, lint)
        lint_copies(brick_paths, lint)
        for brick in brick_paths:
            lint_brick(brick, contracts[brick.name], lint)
    else:
        lint.add(bricks, "bricks directory is missing")
    return lint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    lint = lint_repo(parser.parse_args().root.resolve())
    for item in lint.warnings:
        print(f"WARNING {item}")
    for item in lint.errors:
        print(f"ERROR {item}")
    print(f"brick lint {'failed' if lint.errors else 'passed'}: {len(lint.errors)} errors, {len(lint.warnings)} warnings")
    return bool(lint.errors)


if __name__ == "__main__":
    sys.exit(main())
