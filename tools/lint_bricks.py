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
    "input/adapters",
    "input/config.yml",
    "input/data",
    "runner/AGENTS.md",
    "runner/__init__.py",
    "runner/rng.py",
    "runner/run.py",
    "runner/runs",
    "src/AGENTS.md",
)
# Required paths a lane may leave out, because the lane forbids what they hold.
LANE_OPTIONAL = {"pure": ("input/adapters",)}
CONFIG_DEFAULTS = {
    "runs": 10,
    "saved_examples_per_adapter": 3,
    "max_evidence_bytes": 122_880,
    "max_run_record_bytes": 122_880,
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
# The standard-library modules a pure brick may import: no I/O, clock, randomness, threads or process state.
PURE_STDLIB = {
    "__future__", "abc", "array", "ast", "base64", "binascii", "bisect", "calendar", "cmath", "codecs",
    "collections", "colorsys", "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal", "difflib",
    "enum", "fnmatch", "fractions", "functools", "graphlib", "hashlib", "heapq", "hmac", "html", "io",
    "ipaddress", "itertools", "json", "keyword", "math", "numbers", "operator", "pprint", "re", "reprlib",
    "shlex", "statistics", "string", "struct", "textwrap", "types", "typing", "unicodedata", "warnings", "zlib",
}
# Names a run context goes by in src/ and adapters.
CONTEXT_NAMES = {"ctx", "context", "run_context"}
# An environment variable that switches behavior is a mode the run does not record.
ENV_SWITCH = re.compile(r"(?i)replay|fresh|save|mode|live|offline|record")
IN_MEMORY = {"BytesIO", "StringIO"}
SENSITIVE = {
    "access_token", "api_key", "authorization", "client_secret", "cookie", "cookies",
    "headers", "password", "refresh_token", "secret", "set-cookie", "token", "x-api-key",
}


@dataclass
class Contract:
    version: int = 0
    dependencies: dict[str, str] = field(default_factory=dict)
    owned_state: tuple[str, ...] = ()
    lane: str = "strict"
    deterministic: bool | None = None


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


def mode_override(node: ast.AST) -> bool:
    """True for ``dict(ctx, mode=...)``: src building a context with another mode."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "dict"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in CONTEXT_NAMES
        and any(keyword.arg == "mode" for keyword in node.keywords)
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


def mode_read(node: ast.AST) -> bool:
    """True when ``node`` reads ``mode`` from a run context: ``ctx["mode"]`` or ``ctx.get("mode")``."""
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
        and key.value == "mode"
    )


def lint_contract(brick: Path, lint: Lint) -> Contract:
    path = brick / "contract.py"
    tree = lint.tree(path)
    if not tree:
        return Contract()

    version = assigned_literal(tree, "CONTRACT_VERSION")
    dependencies = assigned_literal(tree, "SIBLING_DEPENDENCIES")
    owned_state = assigned_literal(tree, "OWNED_STATE")
    lane = assigned_literal(tree, "LANE")
    deterministic = assigned_literal(tree, "DETERMINISTIC")
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        lint.add(path, "CONTRACT_VERSION must be a positive integer")
        version = 0
    if not isinstance(dependencies, dict) or not all(
        isinstance(name, str)
        and isinstance(mode, str)
        and mode in {"eventual", "orchestrated"}
        for name, mode in (dependencies.items() if isinstance(dependencies, dict) else ())
    ):
        lint.add(path, "SIBLING_DEPENDENCIES must map names to eventual or orchestrated")
        dependencies = {}
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
    if lane is None:
        lane = "strict"
    elif lane not in LANES:
        lint.add(path, f"LANE must be one of {', '.join(sorted(LANES))}")
        lane = "strict"

    return Contract(version, dict(dependencies), tuple(owned_state), lane, deterministic)


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
        if not {"fresh", "save"}.issubset(options):
            lint.add(brick / "runner/run.py", "run needs keyword-only fresh and save options")
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
                        if parts[1] not in contract.dependencies:
                            lint.add(path, f"sibling dependency {parts[1]!r} is not declared")
            if role == "src" and mode_read(node):
                lint.add(
                    path,
                    "src reads the run mode; each adapter owns saved, fresh and save behavior, "
                    "so label recorded data by what the adapter returned (input/AGENTS.md, Recorded data)",
                    warning=True,
                )
            if role == "src" and mode_override(node):
                lint.add(
                    path,
                    "src overrides the run mode; the mode belongs to the run and its adapters. To use a "
                    "source live or recorded independently, make it its own sibling brick "
                    "(input/AGENTS.md, Recorded data)",
                    warning=True,
                )
            if adapter and env_switch(node):
                lint.add(
                    path,
                    f"adapter switches behavior on environment variable {env_switch(node)!r}; that is a mode "
                    "no run record shows. Put the source in its own sibling brick (it always replays), or "
                    "pass the choice in the brick input",
                    warning=True,
                )
            if role == "src" and isinstance(node, ast.Call) and opens_a_file(node):
                lint.add(
                    path,
                    "src filesystem access must use an adapter (in memory is fine: "
                    "x.open(io.BytesIO(data)) is allowed when the buffer is built in the call)",
                )
            if adapter and isinstance(node, ast.Call) and called_name(node) == "run":
                forwarded = {word.arg for word in node.keywords} & {"fresh", "save"}
                if forwarded:
                    lint.add(
                        path,
                        "sibling run may not receive fresh or save; a sibling always replays. To reach a "
                        "source live from this brick, give this brick its own adapter for it "
                        "(input/AGENTS.md, Sources several bricks share)",
                    )
        if adapter and not imports_sibling and "load_example" not in calls:
            lint.add(
                path,
                "adapter never calls load_example, so it has no saved mode; a file or tool adapter "
                "replays a recorded manifest (input/AGENTS.md, Recorded data)",
                warning=True,
            )

    for dependency in contract.dependencies.keys() - actual_dependencies:
        lint.add(brick / "contract.py", f"declared dependency {dependency!r} has no static adapter import", warning=True)


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


def lint_records(brick: Path, limits: dict[str, int], lint: Lint) -> None:
    data = brick / "input/data"
    counts: dict[str, int] = {}
    for path in sorted(data.rglob("*.json")):
        relative = path.relative_to(data)
        if len(relative.parts) != 2:
            lint.add(path, "evidence path must be <adapter>/<case>.json")
            continue
        adapter, filename = relative.parts
        case = Path(filename).stem
        counts[adapter] = counts.get(adapter, 0) + 1
        size = path.stat().st_size
        if size > limits["max_evidence_bytes"]:
            lint.add(path, "evidence exceeds max_evidence_bytes; store a projection or split it into cases")
        elif size * 5 > limits["max_evidence_bytes"] * 4:
            lint.add(
                path,
                f"evidence is {size * 100 // limits['max_evidence_bytes']}% of max_evidence_bytes; "
                "store a projection of the response or split a session into cases per window "
                "before the next re-record fails",
                warning=True,
            )
        record = read_json(path, lint)
        if not isinstance(record, dict):
            continue
        required = {
            "schema_version",
            "contract_version",
            "adapter",
            "case",
            "capture_run_id",
            "request",
        }
        if required - record.keys():
            lint.add(path, "evidence is missing required fields")
        if record.get("adapter") != adapter or record.get("case") != case:
            lint.add(path, "adapter and case must match the path")
        if ("response" in record) == ("error" in record):
            lint.add(path, "evidence needs exactly one of response or error")
        canonical = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
        if path.read_bytes() != canonical:
            lint.add(path, "evidence JSON is not canonical")
        check_secrets(path, record, lint)
    for adapter, count in counts.items():
        if count > limits["saved_examples_per_adapter"]:
            lint.add(
                data / adapter,
                "too many saved examples; a case per window or per instance is fine, but raise "
                "retention.saved_examples_per_adapter in config.yml deliberately and say why",
            )

    runs = sorted((brick / "runner/runs").glob("*.json"))
    # Test runs (BRICK_RUN_KIND=test) are local, untracked and bounded on their own.
    tracked = [path for path in runs if not path.name.endswith(".test.json")]
    if len(tracked) > limits["runs"]:
        lint.add(brick / "runner/runs", "too many retained run records")
    for path in runs:
        if path.stat().st_size > limits["max_run_record_bytes"]:
            lint.add(path, "run record exceeds max_run_record_bytes")
        read_json(path, lint)


def lint_smokes(brick: Path, contract: Contract, lint: Lint) -> None:
    tests_dir = brick / "runner/tests"
    tests = sorted(tests_dir.glob("test_*.py")) if tests_dir.exists() else []
    if not tests:
        return
    if not (tests_dir / "__init__.py").is_file():
        lint.add(tests_dir / "__init__.py", "smoke-test package marker is missing")
    if contract.lane != "workflow":
        lint.add(tests_dir, "smoke tests belong only to workflow bricks")
    if len(tests) > 3:
        lint.add(tests_dir, "a workflow may have at most three smoke-test files")
    for path in tests:
        tree = lint.tree(path)
        if not tree:
            continue
        if any(
            isinstance(node, ast.Attribute) and node.attr.startswith("skip")
            for node in ast.walk(tree)
        ):
            lint.add(path, "contains a skipped smoke test; it proves no integration", warning=True)
        if "BRICK_RUN_KIND" not in path.read_text(encoding="utf-8"):
            lint.add(
                path,
                "smoke test does not set BRICK_RUN_KIND=test, so its run records land in tracked history; "
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


def lint_strict(brick: Path, contract: Contract, lint: Lint) -> None:
    """The regular lane. Every rule above already applies; this adds none."""


PURE_FORBIDDEN = DIRECT_IO | {"random", "time"}
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
    The pure lane already holds its whole brick to this, so it may not say ``False``.
    """
    if contract.lane == "pure" and contract.deterministic is False:
        lint.add(brick / "contract.py", "a pure brick is deterministic by definition; remove DETERMINISTIC = False")
    if contract.deterministic is not True or contract.lane == "pure":
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


def lint_pure(brick: Path, contract: Contract, lint: Lint) -> None:
    """A pure brick's output is a function of its input and nothing else.

    Adapters are the only sanctioned channel to the outside, so a pure brick
    has none, and therefore no sibling dependencies either. The direct-I/O
    ban every ``src/`` already carries widens to the whole brick, joined by
    ``random`` and ``time``, and by the clock calls the ``src/`` contract has
    always forbidden in prose. ``runner/`` is the one exemption: it writes
    run records and owns ``rng.py``, so it keeps the filesystem and
    randomness the rest of the brick gives up. ``datetime`` stays importable:
    arithmetic on a caller-supplied time is pure; reading ``now()`` is not.
    """
    adapters = sorted((brick / "input/adapters").glob("*.py"))
    for path in adapters:
        lint.add(path, "pure brick may not have adapters")
    if contract.dependencies:
        lint.add(brick / "contract.py", "pure brick may not declare sibling dependencies")
    stdlib = stdlib_names()
    for path in brick.rglob("*.py"):
        relative = path.relative_to(brick).parts
        if relative[0] == "runner":
            continue
        tree = lint.tree(path)
        if not tree:
            continue
        if relative[0] != "src":
            escape_hatches(path, tree, lint, "pure brick")
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for module in imported_modules(node):
                    name = module.lstrip(".").split(".")[0]
                    if (
                        stdlib is not None
                        and name in stdlib
                        and name not in PURE_STDLIB
                        and name not in PURE_FORBIDDEN
                    ):
                        lint.add(path, f"pure brick imports standard-library module {name!r}, which is not on the pure allowlist")
                    if ".".join(module.lstrip(".").split(".")[:2]) in DIRECT_IO_DOTTED and relative[0] != "src":
                        lint.add(path, f"pure brick imports {'.'.join(module.split('.')[:2])!r} outside runner/")
                    # src/ already reports DIRECT_IO under the rule every brick gets.
                    if name in PURE_FORBIDDEN and not (relative[0] == "src" and name in DIRECT_IO):
                        hint = (
                            "; a pure brick has no saved evidence, so delete input/evidence.py"
                            if relative == ("input", "evidence.py")
                            else ""
                        )
                        lint.add(path, f"pure brick imports {name!r} outside runner/{hint}")
            if isinstance(node, ast.Call) and called_name(node) in CLOCK_CALLS:
                lint.add(path, f"pure brick reads the clock with {called_name(node)}()")


# A lane is a declared enforcement class: the rules above plus the ones its
# enforcer adds. A lane may only add rules, never relax one (AGENTS.md:
# "may not relax a parent rule"), and it may not exist without an enforcer,
# which is why the table maps each name to a function rather than listing
# names. A contract with no LANE is in the strict lane.
def lint_workflow(brick: Path, contract: Contract, lint: Lint) -> None:
    """A workflow is invoked as a whole operation, never imported for its parts.

    Two consequences, one enforced here and one in the graph. Here: it is the
    only kind of brick that may carry a process door, ``__main__.py``, and
    that door may import nothing from the brick tree except the brick's own
    ``run`` -- the same rule a sibling adapter lives under, applied to the
    outside world. In the graph: no contract may list a workflow as a sibling
    dependency. Smoke tests, which prove integration through ``run``, belong
    only here; before lanes that was derived from the graph (a brick nothing
    depended on) and now the brick says so.
    """
    door = lint.tree(brick / "__main__.py")
    if not door:
        return
    own_run = {".", ".runner.run", f"bricks.{brick.name}"}
    for node in ast.walk(door):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for module in imported_modules(node):
            inside = module.startswith(".") or module.split(".")[0] == "bricks"
            if not inside:
                top = module.split(".")[0]
                if top in DIRECT_IO - {"os", "pathlib"}:
                    lint.add(
                        brick / "__main__.py",
                        f"__main__ imports direct-I/O module {top!r}; a door parses arguments, calls run and "
                        "prints or writes what run returned; effects belong in an adapter",
                        warning=True,
                    )
                continue
            names = [item.name for item in node.names]
            if not (isinstance(node, ast.ImportFrom) and module in own_run and names == ["run"]):
                lint.add(brick / "__main__.py", "__main__ may import only this brick's run")


LANES = {
    "strict": lint_strict,
    "pure": lint_pure,
    "workflow": lint_workflow,
}


def lint_brick(brick: Path, contract: Contract, lint: Lint) -> None:
    for relative in REQUIRED:
        # A pure brick has no adapters, and Git cannot track the empty folder.
        if relative in LANE_OPTIONAL.get(contract.lane, ()):
            continue
        if not (brick / relative).exists():
            lint.add(brick / relative, "required brick path is missing")
    config_path = brick / "input/config.yml"
    config = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    limits = {key: config_number(config, key, lint, config_path) for key in CONFIG_DEFAULTS}
    lint_python(brick, contract, lint)
    lint_records(brick, limits, lint)
    lint_smokes(brick, contract, lint)
    if contract.lane != "workflow" and (brick / "__main__.py").exists():
        lint.add(brick / "__main__.py", "only a workflow brick may have __main__.py")
    lint_deterministic(brick, contract, lint)
    LANES[contract.lane](brick, contract, lint)


COPY_MARKER = re.compile(r"^#\s*BOILERPLATE:\s*([a-z0-9_.-]+)@(\d+)\s*$")


def lint_copies(bricks: list[Path], lint: Lint) -> None:
    """Warn when copies of one boilerplate file have drifted apart.

    Some files are copied into every brick on purpose (``input/evidence.py``,
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


def lint_graph(
    bricks: list[Path], contracts: dict[str, Contract], lint: Lint
) -> None:
    names = set(contracts)
    owners: dict[str, str] = {}
    graph: dict[str, set[str]] = {name: set() for name in names}

    for name, contract in contracts.items():
        for dependency in contract.dependencies:
            if dependency == name:
                lint.add(next(brick for brick in bricks if brick.name == name) / "contract.py", "brick may not depend on itself")
            elif dependency not in names:
                lint.add(next(brick for brick in bricks if brick.name == name) / "contract.py", f"unknown sibling dependency {dependency!r}")
            else:
                if contracts[dependency].lane == "workflow":
                    lint.add(
                        next(brick for brick in bricks if brick.name == name) / "contract.py",
                        f"sibling dependency {dependency!r} is a workflow brick; a workflow is invoked as a whole, not depended on",
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
            lint.add(lint.root / "bricks", f"sibling dependency cycle: {cycle}")
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
