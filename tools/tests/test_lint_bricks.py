"""Tests for tools/lint_bricks.py, standard library only.

Every test builds a throwaway repository from the committed boilerplate,
mutates one thing, and asserts on the exact message the linter reports.
The boilerplate itself must lint clean; that is the first test.
"""

from __future__ import annotations

import ast
from pathlib import Path
import shutil
import tempfile
import unittest

from tools import brick_contract, lint_bricks

ROOT = Path(__file__).resolve().parents[2]


class Fixture(unittest.TestCase):
    """A copy of the boilerplate under a temporary root, one brick per name."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="brick-lint-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(ROOT / "AGENTS.md", self.root / "AGENTS.md")
        (self.root / "bricks").mkdir()
        shutil.copy(ROOT / "bricks/AGENTS.md", self.root / "bricks/AGENTS.md")
        shutil.copy(ROOT / "bricks/__init__.py", self.root / "bricks/__init__.py")

    def brick(self, name: str = "example_brick") -> Path:
        target = self.root / "bricks" / name
        shutil.copytree(ROOT / "bricks/example_brick", target, ignore=shutil.ignore_patterns("__pycache__"))
        return target

    def contract(self, brick: Path, **values: str) -> None:
        """Rewrite one or more top-level assignments in the brick's contract (or add them)."""
        path = brick / "contract.py"
        text = path.read_text(encoding="utf-8")
        for name, value in values.items():
            lines = text.splitlines(keepends=True)
            for node in ast.parse(text).body:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    lines[node.lineno - 1 : node.end_lineno] = [f"{name} = {value}\n"]
                    break
            else:
                lines.append(f"{name} = {value}\n")
            text = "".join(lines)
        path.write_text(text, encoding="utf-8")

    def declare(self, brick: Path, name: str, kind: str = "source", **fields: object) -> None:
        """Add one port to the brick's PORTS (or replace it)."""
        ports = brick_contract.brick_ports(brick)
        ports[name] = {"kind": kind, **fields}
        self.contract(brick, PORTS=repr(ports))

    def adapter(self, brick: Path, name: str, text: str, kind: str = "source", **fields: object) -> None:
        """Write input/adapters/<name>.py and declare the port it implements."""
        (brick / "input/adapters" / f"{name}.py").write_text(text, encoding="utf-8")
        if name not in brick_contract.brick_ports(brick):
            if kind == "joint":
                fields = {"policy": "eventual", "pin": 1, **fields}
            else:
                fields = {"epoch": 1, **fields}
            self.declare(brick, name, kind, **fields)

    def joint(self, brick: Path, name: str, policy: str = "eventual", pin: int = 1) -> None:
        """Declare a joint to sibling ``name`` and write its adapter."""
        self.adapter(
            brick, name,
            f"from bricks.{name} import run\n\n\ndef fetch(request, ctx):\n    return run(request)\n",
            "joint", policy=policy, pin=pin,
        )

    def pure_brick(self, name: str = "example_brick") -> Path:
        """The template with its one adapter and the tape loader removed."""
        brick = self.brick(name)
        (brick / "input/adapters/example_source.py").unlink()
        (brick / "input/tape.py").unlink()
        self.contract(brick, LANE='"pure"', PORTS="{}")
        return brick

    def errors(self) -> list[str]:
        return lint_bricks.lint_repo(self.root).errors

    def warnings(self) -> list[str]:
        return lint_bricks.lint_repo(self.root).warnings

    def assertError(self, fragment: str) -> None:
        errors = self.errors()
        self.assertTrue(any(fragment in item for item in errors), f"{fragment!r} not in {errors}")

    def assertClean(self) -> None:
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual(lint.errors, [])
        self.assertEqual(lint.warnings, [])


class BoilerplateTests(Fixture):
    def test_committed_boilerplate_lints_clean(self) -> None:
        self.assertClean()
        self.assertEqual(lint_bricks.lint_repo(ROOT).errors, [])

    def test_fixture_copy_lints_clean(self) -> None:
        self.brick()
        self.assertClean()

    def test_src_may_not_import_direct_io(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text("import requests\n", encoding="utf-8")
        self.assertError("src imports direct-I/O module 'requests'")


class LaneTests(Fixture):
    def test_absent_lane_is_strict(self) -> None:
        brick = self.brick()
        path = brick / "contract.py"
        kept = [line for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
                if not line.startswith("LANE")]
        path.write_text("".join(kept), encoding="utf-8")
        self.assertNotIn("LANE =", path.read_text(encoding="utf-8"))
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual(lint.errors, [])
        self.assertEqual(lint_bricks.lint_contract(brick, lint).lane, "strict")

    def test_every_lane_has_an_enforcer(self) -> None:
        self.assertIn("strict", lint_bricks.LANES)
        for lane, enforcer in lint_bricks.LANES.items():
            self.assertTrue(callable(enforcer), lane)

    def test_declared_strict_lints_clean(self) -> None:
        self.contract(self.brick(), LANE='"strict"')
        self.assertClean()

    def test_unknown_lane_is_an_error(self) -> None:
        self.contract(self.brick(), LANE='"permissive"')
        self.assertError("LANE must be one of")

    def test_non_string_lane_is_an_error(self) -> None:
        self.contract(self.brick(), LANE="1")
        self.assertError("LANE must be one of")


class ModeReadTests(Fixture):
    def test_src_reading_the_plan_warns_and_does_not_fail(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def decide(ctx):\n    return ctx['plan'] == () or ctx.get('plan')\n", encoding="utf-8"
        )
        warnings = [item for item in self.warnings() if "src/logic.py: src reads the run plan" in item]
        self.assertEqual(len(warnings), 2)
        self.assertEqual(self.errors(), [])

    def test_adapter_reading_the_plan_is_its_job(self) -> None:
        self.brick()
        self.assertClean()

    def test_src_reading_a_plan_field_of_its_input_does_not_warn(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def decide(inputs, ctx):\n    return inputs['plan'], ctx['config'], ctx['run_id']\n",
            encoding="utf-8",
        )
        self.assertClean()


SIBLING_ADAPTER = (
    "from bricks.other import run\n\n\ndef fetch(request, ctx):\n    return run(request)\n"
)
ADAPTER_WITH_REPLAY = (
    "from ..tape import load\n\n\n"
    "def fetch(request, ctx):\n{body}    return load('feed', 'default', request)['response']\n"
)




SESSION_ADAPTER = (
    "from ..tape import session\n\n\ndef open_session(ctx):\n    return session(ctx, 'game', 'year')\n"
)


class LabelLintTests(Fixture):
    def test_a_session_on_a_source_with_no_label_warns(self) -> None:
        brick = self.brick()
        self.adapter(brick, "game", SESSION_ADAPTER, epoch=1)
        self.assertTrue(any("game.py opens a session but source 'game' has no label in PORTS" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_a_session_on_a_labelled_source_is_clean(self) -> None:
        brick = self.brick()
        self.adapter(brick, "game", SESSION_ADAPTER, epoch=1, label={"version": "1"})
        self.assertClean()

    def test_a_label_must_be_a_non_empty_dict(self) -> None:
        brick = self.brick()
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1, "label": {}}}')
        self.assertError("source 'example_source' label must be a non-empty dict")
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1, "label": "v2"}}')
        self.assertError("source 'example_source' label must be a non-empty dict")

    def test_a_source_needs_an_epoch(self) -> None:
        self.contract(self.brick(), PORTS='{"example_source": {"kind": "source"}}')
        self.assertError("source 'example_source' needs epoch")
        self.contract(self.brick("b"), PORTS='{"example_source": {"kind": "source", "epoch": 0}}')
        self.assertError("bricks/b/contract.py: source 'example_source' needs epoch")

    def test_followable_must_be_a_bool(self) -> None:
        self.contract(self.brick(), PORTS='{"example_source": {"kind": "source", "epoch": 1, "followable": "yes"}}')
        self.assertError("source 'example_source' followable must be True or False")


class FollowLintTests(Fixture):
    FOLLOWING = (
        "from bricks.game import run\nfrom ..tape import follow\n\n\n"
        "def step(ctx, request):\n    flags = follow(ctx, 'game')\n"
        "    return run(request, fresh=flags['fresh'], record=flags['record'])\n"
    )

    def pair(self, *, adapter: str | None = None, policy: str = "orchestrated", followable: bool = True, follows: tuple = ("feed",)) -> tuple[Path, Path]:
        game = self.brick("game")
        self.adapter(game, "feed", ADAPTER_WITH_REPLAY.format(body=""), epoch=1, followable=followable)
        flow = self.brick("flow")
        self.adapter(flow, "game", adapter or self.FOLLOWING, "joint", policy=policy, pin=1, **({"follow": follows} if follows else {}))
        return flow, game

    def test_a_declared_helper_fed_call_lints_clean(self) -> None:
        self.pair()
        self.assertClean()

    def test_the_helper_can_be_called_inline(self) -> None:
        self.pair(adapter=self.FOLLOWING.replace("    flags = follow(ctx, 'game')\n", "").replace("flags['fresh']", "follow(ctx, 'game')['fresh']").replace("flags['record']", "follow(ctx, 'game')['record']"))
        self.assertClean()

    def test_a_hard_coded_flag_is_an_error(self) -> None:
        self.pair(adapter=self.FOLLOWING.replace("fresh=flags['fresh']", "fresh=True"))
        self.assertError("fresh= for 'game' must be follow(ctx, 'game')['fresh'], not a value written here")

    def test_expanded_keyword_arguments_are_an_error(self) -> None:
        self.pair(adapter=self.FOLLOWING.replace("fresh=flags['fresh'], record=flags['record']", "**flags"))
        self.assertError("may not receive expanded keyword arguments")
        self.assertError("must give both fresh and record")

    def test_giving_only_one_flag_is_an_error(self) -> None:
        self.pair(adapter=self.FOLLOWING.replace(", record=flags['record']", ""))
        self.assertError("following 'game' must give both fresh and record")

    def test_flags_made_for_another_joint_are_an_error(self) -> None:
        self.pair(adapter=self.FOLLOWING.replace("follow(ctx, 'game')", "follow(ctx, 'other')"))
        self.assertError("follow for 'other', which has no follow list in PORTS")
        self.assertError("fresh= for 'game' must be follow(ctx, 'game')['fresh']")

    def test_following_a_source_that_is_not_followable_is_an_error(self) -> None:
        self.pair(followable=False)
        self.assertError("joint 'game' follows 'feed', which is not a followable source of 'game'")

    def test_following_an_unknown_source_is_an_error(self) -> None:
        self.pair(follows=("nothing",))
        self.assertError("joint 'game' follows 'nothing', which is not a followable source of 'game'")

    def test_follow_needs_an_orchestrated_joint(self) -> None:
        self.pair(policy="eventual")
        self.assertError("joint 'game' follows sources, so its policy must be 'orchestrated'")

    def test_follow_must_list_names(self) -> None:
        flow = self.brick("flow")
        self.brick("game")
        self.adapter(flow, "game", SIBLING_ADAPTER.replace("other", "game"), "joint", policy="orchestrated", pin=1, follow=())
        self.assertError("joint 'game' follow must list the sibling's followable source names")

    def test_a_joint_with_no_follow_gives_the_sibling_no_flags(self) -> None:
        self.pair(follows=())
        self.assertError("a sibling run may not receive fresh or record unless its joint lists follow in PORTS")

    def test_an_ordinary_sibling_still_gets_no_flags(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.adapter(
            brick, "other",
            "from bricks.other import run\n\n\ndef f(request):\n    return run(request, fresh=True)\n",
            "joint", policy="eventual", pin=1,
        )
        self.assertError("a sibling run may not receive fresh or record unless its joint lists follow")

    def test_the_old_save_flag_is_never_accepted(self) -> None:
        self.pair(adapter=self.FOLLOWING.replace("record=flags['record']", "save=flags['record']"))
        self.assertError("must give both fresh and record")


class PortTests(Fixture):
    def test_the_template_declares_its_one_source_port(self) -> None:
        brick = self.brick()
        self.assertEqual(brick_contract.brick_ports(brick), {"example_source": {"kind": "source", "epoch": 1}})
        self.assertClean()

    def test_ports_must_be_declared(self) -> None:
        brick = self.brick()
        path = brick / "contract.py"
        kept = ast.unparse(ast.parse(path.read_text(encoding="utf-8")))
        path.write_text("\n".join(line for line in kept.splitlines() if not line.startswith(("PORTS", "    'example_source'"))), encoding="utf-8")
        self.assertError("PORTS is not declared")

    def test_ports_must_be_a_dict_literal(self) -> None:
        self.contract(self.brick(), PORTS="make_ports()")
        self.assertError("PORTS must be a dict literal")

    def test_a_port_needs_a_valid_kind_and_name(self) -> None:
        brick = self.brick()
        self.contract(brick, PORTS='{"Bad-Name": {"kind": "source", "epoch": 1}, "other": {"kind": "door"}, "third": 3}')
        self.assertError("port name 'Bad-Name' must be lowercase letters")
        self.assertError("port 'other' needs kind 'source' or 'joint'")
        self.assertError("port 'third' needs kind 'source' or 'joint'")

    def test_a_port_takes_only_its_own_fields(self) -> None:
        brick = self.brick()
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1, "policy": "eventual"}}')
        self.assertError("port 'example_source' has field 'policy', which a source port does not take")

    def test_a_joint_needs_a_policy_and_a_pin(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1}, "other": {"kind": "joint"}}')
        self.assertError("joint 'other' needs policy 'eventual' or 'orchestrated'")
        self.assertError("joint 'other' needs pin")

    def test_a_joint_with_its_adapter_lints_clean(self) -> None:
        self.brick("other")
        self.joint(self.brick(), "other", "orchestrated")
        self.assertClean()

    def test_an_adapter_must_implement_a_declared_port(self) -> None:
        brick = self.brick()
        (brick / "input/adapters/extra.py").write_text("x = 1\n", encoding="utf-8")
        self.assertError("adapter extra.py has no port 'extra' in PORTS")

    def test_a_declared_port_needs_its_adapter_file(self) -> None:
        brick = self.brick()
        self.declare(brick, "feed", epoch=1)
        self.assertError("port 'feed' has no adapter file input/adapters/feed.py")

    def test_a_joint_adapter_must_be_named_for_its_joint(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.adapter(brick, "link", SIBLING_ADAPTER, "joint", policy="eventual", pin=1)
        self.assertError("joint 'other' is not declared in PORTS")
        self.declare(brick, "other", "joint", policy="eventual", pin=1)
        self.assertError("the adapter for joint 'other' must be named other.py")

    def test_an_undeclared_joint_is_an_error(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.adapter(brick, "other", SIBLING_ADAPTER, epoch=1)
        self.assertError("joint 'other' is not declared in PORTS")

    def test_a_joint_may_not_name_a_missing_brick_or_itself(self) -> None:
        brick = self.brick("alone")
        self.adapter(brick, "ghost", "x = 1\n", "joint", policy="eventual", pin=1)
        self.declare(brick, "alone", "joint", policy="eventual", pin=1)
        self.assertError("joint 'ghost' names a brick that does not exist")
        self.assertError("brick may not depend on itself")

    def test_a_pin_must_match_the_siblings_interface_version(self) -> None:
        other = self.brick("other")
        self.contract(other, CONTRACT_VERSION="2")
        self.joint(self.brick(), "other", pin=1)
        self.assertError("joint 'other' pins version 1, but 'other' is at version 2; update the adapter and the pin together")
        self.joint(self.brick("third"), "other", pin=2)
        self.assertFalse([e for e in self.errors() if "third" in e])

    def test_a_sibling_never_receives_fresh_or_save(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.adapter(
            brick, "other",
            "from bricks.other import run\n\n\ndef fetch(request, ctx):\n    return run(request, fresh=True)\n",
            "joint", policy="eventual", pin=1,
        )
        self.assertError("a sibling run may not receive fresh or record")

    def test_a_declared_joint_with_no_import_warns(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.adapter(brick, "other", "def fetch(request, ctx):\n    return request\n", "joint", policy="eventual", pin=1)
        self.assertTrue(any("declared joint 'other' has no static adapter import" in w for w in self.warnings()))

    def test_the_old_manifest_declarations_are_errors_that_name_the_converter(self) -> None:
        brick = self.brick()
        self.contract(brick, SIBLING_DEPENDENCIES="{}", LIVE_ADAPTER="False", MODE_PASSING="()")
        self.assertError("SIBLING_DEPENDENCIES, LIVE_ADAPTER, MODE_PASSING are declarations of the old manifest format")
        self.assertError("tools/convert_ports.py")

    def test_a_pure_brick_may_not_declare_ports(self) -> None:
        brick = self.pure_brick()
        self.contract(brick, PORTS='{"feed": {"kind": "source", "epoch": 1}}')
        self.assertError("pure brick may not declare ports")


class DispatcherTests(Fixture):
    def input_class(self, brick: Path, header: str, fields: list[str]) -> None:
        path = brick / "contract.py"
        text = path.read_text(encoding="utf-8")
        body = "".join(f"    {name}: str\n" for name in fields) or '    """x"""\n'
        text = text.replace('class BrickInput(TypedDict):\n    """Input accepted by run(). Replace with domain fields."""\n', f"{header}\n{body}")
        path.write_text(text, encoding="utf-8")

    def test_dispatcher_field_warns(self) -> None:
        for name in ("op", "flow", "action", "operation", "kind"):
            with self.subTest(name=name):
                brick = self.brick(f"b_{name}")
                self.input_class(brick, "class BrickInput(TypedDict):", [name])
        warnings = [w for w in self.warnings() if "selects an operation" in w]
        self.assertEqual(len(warnings), 5)
        self.assertEqual(self.errors(), [])

    def test_pure_brick_is_held_to_it_too(self) -> None:
        brick = self.pure_brick()
        self.input_class(brick, "class BrickInput(TypedDict, total=False):", ["op", "text"])
        self.assertTrue(any("BrickInput.op selects an operation" in w for w in self.warnings()))

    def test_many_optional_fields_warn(self) -> None:
        brick = self.brick()
        self.input_class(brick, "class BrickInput(TypedDict, total=False):", [f"f{i}" for i in range(7)])
        self.assertTrue(any("7 fields and all are optional" in w for w in self.warnings()))

    def test_six_optional_fields_or_many_required_ones_do_not(self) -> None:
        brick = self.brick("six")
        self.input_class(brick, "class BrickInput(TypedDict, total=False):", [f"f{i}" for i in range(6)])
        brick = self.brick("many")
        self.input_class(brick, "class BrickInput(TypedDict):", [f"g{i}" for i in range(9)])
        self.assertClean()

    def test_template_input_is_clean(self) -> None:
        self.brick()
        self.assertClean()


class ModeBlindFunctionTests(Fixture):
    ADAPTER = (
        "from pathlib import Path\nfrom ..tape import load\n\n\n"
        "def exists(template_dir, seed):\n    return (Path(template_dir) / f'seed-{seed}.save').is_file()\n\n\n"
        "def store(ctx, template_dir, seed):\n    return load('template_files', 'c', {})\n\n\n"
        "def stats(pid):\n    return Path(f'/proc/{pid}').exists()\n"
    )

    def test_io_function_without_context_called_from_src_warns(self) -> None:
        brick = self.brick()
        self.adapter(brick, "template_files", self.ADAPTER)
        (brick / "src/logic.py").write_text(
            "from ..input.adapters import template_files\n\n\ndef execute(inputs, ctx):\n    return template_files.exists('d', 1)\n",
            encoding="utf-8",
        )
        warnings = [w for w in self.warnings() if "does I/O and takes no run context" in w]
        self.assertEqual(len(warnings), 1)
        self.assertIn("template_files.py: exists()", warnings[0])
        self.assertEqual(self.errors(), [])

    def test_helper_used_only_inside_the_adapter_is_not_flagged(self) -> None:
        brick = self.brick()
        self.adapter(brick, "template_files", self.ADAPTER)
        self.assertFalse([w for w in self.warnings() if "takes no run context" in w])

    def test_importing_the_function_by_name_counts_as_a_call_from_outside(self) -> None:
        brick = self.brick()
        self.adapter(brick, "template_files", self.ADAPTER)
        (brick / "src/logic.py").write_text(
            "from ..input.adapters.template_files import exists\n\n\ndef execute(inputs, ctx):\n    return exists('d', 1)\n",
            encoding="utf-8",
        )
        self.assertTrue(any("exists() does I/O" in w for w in self.warnings()))

    def test_function_that_takes_the_context_is_fine(self) -> None:
        brick = self.brick()
        self.adapter(brick, "template_files", self.ADAPTER.replace("def exists(template_dir, seed)", "def exists(ctx, template_dir, seed)"))
        (brick / "src/logic.py").write_text(
            "from ..input.adapters import template_files\n\n\ndef execute(inputs, ctx):\n    return template_files.exists(ctx, 'd', 1)\n",
            encoding="utf-8",
        )
        self.assertFalse([w for w in self.warnings() if "takes no run context" in w])


class ContractBytesTests(Fixture):
    def test_bytes_in_a_contract_warns(self) -> None:
        brick = self.brick()
        path = brick / "contract.py"
        text = path.read_text(encoding="utf-8")
        text = text.replace('class BrickInput(TypedDict):\n    """Input accepted by run(). Replace with domain fields."""\n', 'class BrickInput(TypedDict, total=False):\n    frames: dict[str, bytes]\n    one: bytes | None\n')
        text = text.replace('class BrickOutput(TypedDict):\n    """Output returned by run(). Replace with domain fields."""\n', 'class BrickOutput(TypedDict):\n    files: dict[str, str | bytearray]\n')
        path.write_text(text, encoding="utf-8")
        warnings = [w for w in self.warnings() if "holds bytes" in w]
        self.assertEqual(len(warnings), 3)
        self.assertTrue(any("BrickInput.frames" in w for w in warnings) and any("BrickOutput.files" in w for w in warnings))
        self.assertEqual(self.errors(), [])

    def test_json_fields_do_not_warn(self) -> None:
        self.brick()
        self.assertClean()


class IoBanTests(Fixture):
    def src(self, brick: Path, text: str, name: str = "helpers.py") -> None:
        (brick / "src" / name).write_text(text, encoding="utf-8")

    def test_modules_that_do_io_are_banned_in_src(self) -> None:
        brick = self.brick()
        modules = ["http.client", "tempfile", "glob", "ssl", "asyncio", "ctypes", "multiprocessing", "shelve", "webbrowser", "builtins"]
        self.src(brick, "".join(f"import {name}\n" for name in modules))
        for name in modules:
            self.assertError(f"src imports direct-I/O module {name.split('.')[0]!r}")

    def test_logging_handlers_are_banned_but_logging_is_not(self) -> None:
        brick = self.brick()
        self.src(brick, "import logging\nimport logging.handlers\n")
        errors = [item for item in self.errors() if "helpers.py" in item]
        self.assertEqual(errors, ["bricks/example_brick/src/helpers.py: src imports direct-I/O module 'logging.handlers'"])

    def test_dynamic_import_eval_and_exec_are_banned_in_src(self) -> None:
        brick = self.brick()
        self.src(brick, "def a():\n    return __import__('os')\n\n\ndef b(x):\n    return eval(x)\n\n\ndef c(x):\n    exec(x)\n")
        for name in ("__import__", "eval", "exec"):
            self.assertError(f"src may not call {name}()")

    def test_importlib_is_banned_in_src(self) -> None:
        self.src(self.brick(), "import importlib\n")
        self.assertError("src imports direct-I/O module 'importlib'")

    def test_sys_modules_and_builtins_are_banned_in_src(self) -> None:
        brick = self.brick()
        self.src(brick, "import sys\n\n\ndef a():\n    return sys.modules['os']\n\n\ndef b():\n    return __builtins__\n")
        self.assertError("src may not use sys.modules")
        self.assertError("src may not use __builtins__")

    def test_importing_sys_for_other_things_is_fine(self) -> None:
        brick = self.brick()
        self.src(brick, "import sys\n\n\ndef limit():\n    return sys.maxsize\n")
        self.assertClean()

    def test_importlib_in_an_adapter_is_a_hidden_dependency(self) -> None:
        brick = self.brick()
        self.adapter(brick, "dyn", "import importlib\nfrom ..tape import load\n\n\ndef f(ctx):\n    load('dyn', 'b', {})\n    return importlib.import_module('bricks.other')\n",
            encoding="utf-8",
        )
        self.assertError("dyn.py: dynamic imports hide brick boundaries")

    def test_runner_may_still_import_what_it_needs(self) -> None:
        self.brick()
        self.assertClean()

    def test_pure_brick_may_import_only_allowlisted_standard_library(self) -> None:
        brick = self.pure_brick()
        self.src(brick, "import json\nimport re\nimport io\nimport operator\nimport sys\nimport logging\nimport inspect\n")
        self.assertError("pure brick imports standard-library module 'sys', which is not on the pure allowlist")
        self.assertError("standard-library module 'logging'")
        self.assertError("standard-library module 'inspect'")
        self.assertFalse([item for item in self.errors() if "'json'" in item or "'re'" in item or "'io'" in item or "'operator'" in item])

    def test_pure_brick_may_use_third_party_libraries(self) -> None:
        brick = self.pure_brick()
        self.src(brick, "import numpy\n")
        self.assertClean()

    def test_pure_brick_bans_escape_hatches_outside_src_too(self) -> None:
        brick = self.pure_brick()
        (brick / "input/loader.py").write_text("def f(x):\n    return __import__(x)\n")
        self.assertError("loader.py: pure brick may not call __import__()")

    def test_pure_brick_bans_logging_handlers_outside_src(self) -> None:
        brick = self.pure_brick()
        (brick / "input/loader.py").write_text("import logging.handlers\n", encoding="utf-8")
        self.assertError("loader.py: pure brick imports 'logging.handlers' outside runner/")

    def test_door_importing_io_modules_warns(self) -> None:
        brick = self.brick()
        self.contract(brick, LANE='"workflow"')
        (brick / "__main__.py").write_text(
            "import argparse\nimport os\nimport subprocess\nimport shutil\nfrom pathlib import Path\n\nfrom bricks.example_brick import run\n",
            encoding="utf-8",
        )
        warnings = [w for w in self.warnings() if "__main__.py" in w]
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("'subprocess'" in w for w in warnings) and any("'shutil'" in w for w in warnings))
        self.assertEqual(self.errors(), [])


class AdapterExperienceTests(Fixture):
    def test_env_switch_in_an_adapter_warns(self) -> None:
        brick = self.brick()
        self.adapter(
            brick, "feed",
            ADAPTER_WITH_REPLAY.format(body="    if os.environ.get('FEED_REPLAY'):\n        pass\n")
            .replace("from ..tape", "import os\nfrom ..tape"),
        )
        self.assertTrue(any("environment variable 'FEED_REPLAY'" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_getenv_and_subscript_forms_warn(self) -> None:
        brick = self.brick()
        self.adapter(
            brick, "feed",
            "import os\nfrom ..tape import load\n\n\ndef fetch(request, ctx):\n"
            "    a = os.getenv('FEED_LIVE')\n    b = os.environ['FEED_OFFLINE']\n"
            "    return load('feed', 'default', request)\n",
        )
        hits = [w for w in self.warnings() if "environment variable" in w]
        self.assertEqual(len(hits), 2)

    def test_credentials_and_paths_in_the_environment_are_fine(self) -> None:
        brick = self.brick()
        self.adapter(
            brick, "feed",
            "import os\nfrom ..tape import load\n\n\ndef fetch(request, ctx):\n"
            "    token = os.environ.get('FEED_TOKEN')\n    root = os.getenv('FEED_ROOT')\n"
            "    return load('feed', 'default', request)\n",
        )
        self.assertTrue(all("environment variable" not in w for w in self.warnings()))

    def test_adapter_with_no_saved_mode_warns(self) -> None:
        brick = self.brick()
        self.adapter(brick, "tool", "import subprocess\n\n\ndef encode(request, ctx):\n    return {}\n")
        self.assertTrue(any("tool.py: adapter for source 'tool' never plays a tape" in w for w in self.warnings()))

    def test_once_and_session_count_as_a_replay(self) -> None:
        brick = self.brick()
        self.adapter(brick, "a", "from ..tape import once\n\n\ndef f(ctx):\n    return once(ctx, 'a', 'c', {}, dict)\n")
        self.adapter(brick, "b", "from ..tape import session\n\n\ndef f(ctx):\n    return session(ctx, 'b', 'c')\n", label={"v": 1})
        self.assertTrue(all("never plays a tape" not in w for w in self.warnings()))

    def test_an_adapter_may_only_use_the_tape_of_its_own_port(self) -> None:
        brick = self.brick()
        self.adapter(brick, "a", "from ..tape import once\n\n\ndef f(ctx):\n    return once(ctx, 'b', 'c', {}, dict)\n")
        self.assertError("adapter a.py uses the tape of source 'b'; one adapter file implements one port, so use 'a'")

    def test_sibling_adapter_needs_no_saved_mode_of_its_own(self) -> None:
        self.brick("other")
        self.joint(self.brick(), "other")
        self.assertTrue(all("never plays a tape" not in w for w in self.warnings()))

    def test_src_overriding_the_plan_warns(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def second(ctx):\n    return dict(ctx, plan={'fresh': ('x',)})\n", encoding="utf-8"
        )
        self.assertTrue(any("src overrides the run plan" in w for w in self.warnings()))

    def test_in_memory_open_is_allowed_in_src(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "import io\n\n\ndef decode(image, data):\n    return image.open(io.BytesIO(data))\n"
            "\n\ndef text(image, data):\n    return image.open(StringIO(data))\n",
            encoding="utf-8",
        )
        self.assertTrue(all("src/logic.py" not in item for item in self.errors()))

    def test_open_on_anything_else_is_still_a_file_access(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def a(image, path):\n    return image.open(path)\n"
            "\n\ndef b(path):\n    return open(path)\n"
            "\n\ndef c(image, buf):\n    return image.open(buf)\n"
            "\n\ndef d(data):\n    return open(BytesIO(data))\n",
            encoding="utf-8",
        )
        hits = [item for item in self.errors() if "src filesystem access must use an adapter" in item]
        self.assertEqual(len(hits), 4)


class TakeLintTests(Fixture):
    def take(self, brick: Path, name: str, padding: int = 10, source: str = "example_source", **changes: object) -> Path:
        import json

        record = {
            "epoch": 1, "recorded_in": "r1", "request": {}, "response": {"pad": "x" * padding},
            "schema_version": 2, "source": source, "take": name,
        }
        record.update(changes)
        folder = brick / "input/tapes" / source
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_a_take_near_the_limit_warns(self) -> None:
        brick = self.brick()
        self.take(brick, "big", 105_000)
        self.assertTrue(any("big.json: take is 8" in w and "% of max_take_bytes" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_a_small_take_is_quiet(self) -> None:
        self.take(self.brick(), "small", 1_000)
        self.assertClean()

    def test_a_take_over_the_limit_is_an_error_with_a_hint(self) -> None:
        self.take(self.brick(), "huge", 130_000)
        self.assertError("take exceeds max_take_bytes; store a projection or split the session into more takes")

    def test_too_many_takes_says_it_is_a_deliberate_choice(self) -> None:
        brick = self.brick()
        for index in range(4):
            self.take(brick, f"w{index}")
        self.assertError("raise retention.takes_per_tape in config.yml deliberately")

    def test_a_take_needs_its_fields_and_a_matching_path(self) -> None:
        brick = self.brick()
        path = self.take(brick, "w0")
        import json

        record = json.loads(path.read_text(encoding="utf-8"))
        del record["recorded_in"]
        record["take"] = "other"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.assertError("take is missing required fields")
        self.assertError("source and take must match the path")

    def test_a_take_needs_a_response_or_an_error(self) -> None:
        brick = self.brick()
        self.take(brick, "both", error={"type": "X"})
        self.assertError("a take needs exactly one of response or error")

    def test_a_take_must_be_canonical_and_free_of_secrets(self) -> None:
        brick = self.brick()
        path = self.take(brick, "w0", request={"token": "abc"})
        self.assertError("sensitive field 'token' is not redacted")
        path.write_text(path.read_text(encoding="utf-8").replace('  "', ' "', 1), encoding="utf-8")
        self.assertError("take JSON is not canonical")

    def test_a_take_path_must_be_source_slash_take(self) -> None:
        brick = self.brick()
        (brick / "input/tapes/loose.json").write_text("{}\n", encoding="utf-8")
        self.assertError("a take path must be <source>/<take>.json")

    def test_a_tape_folder_with_no_source_port_is_an_error(self) -> None:
        brick = self.brick()
        self.take(brick, "w0", source="ghost")
        self.assertError("tape folder 'ghost' has no source port in PORTS")

    def test_a_take_at_another_epoch_is_stale_without_running_anything(self) -> None:
        brick = self.brick()
        self.take(brick, "w0", epoch=1)
        self.assertClean()
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 2}}')
        self.assertError("take recorded at epoch 1, port 'example_source' is at epoch 2; re-record it")

    def test_a_take_with_another_label_is_stale_and_names_the_field(self) -> None:
        brick = self.brick()
        self.take(brick, "w0", request={"label": {"api": "v2"}, "q": 1})
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1, "label": {"api": "v3"}}}')
        self.assertError("take label differs from port 'example_source' (api: recorded \"v2\", port \"v3\"); re-record it")

    def test_a_matching_label_is_clean_and_a_label_the_port_lacks_is_not(self) -> None:
        brick = self.brick()
        self.take(brick, "w0", request={"label": {"api": "v2"}})
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1, "label": {"api": "v2"}}}')
        self.assertClean()
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1}}')
        self.assertError("take label differs from port 'example_source' (api: recorded \"v2\", port \"nothing\")")

    def test_an_unlabelled_take_under_a_labelled_port_is_stale(self) -> None:
        brick = self.brick()
        self.take(brick, "w0")
        self.contract(brick, PORTS='{"example_source": {"kind": "source", "epoch": 1, "label": {"api": "v2"}}}')
        self.assertError("take label differs from port 'example_source' (api: recorded \"nothing\", port \"v2\")")

    def test_the_old_take_format_and_paths_point_at_the_converter(self) -> None:
        brick = self.brick()
        old = brick / "input/data/example_source"
        old.mkdir(parents=True)
        (old / "default.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
        self.assertError("input/data: old path of the previous format; run python3 tools/convert_ports.py once")
        (brick / "input/evidence.py").write_text("x = 1\n", encoding="utf-8")
        self.assertError("input/evidence.py: old path of the previous format")
        schema_one = brick / "input/tapes/example_source"
        schema_one.mkdir(parents=True, exist_ok=True)
        (schema_one / "default.json").write_text('{"schema_version": 1}\n', encoding="utf-8")
        self.assertError("take is not in schema 2 (the old format?); run python3 tools/convert_ports.py once")


class RunRecordTests(Fixture):
    def test_test_records_do_not_count_toward_retention(self) -> None:
        brick = self.brick()
        runs = brick / "runner/runs"
        for index in range(12):
            (runs / f"20260101-0000{index:02d}.test.json").write_text("{}\n", encoding="utf-8")
        self.assertClean()

    def test_tracked_records_still_have_a_limit(self) -> None:
        brick = self.brick()
        runs = brick / "runner/runs"
        for index in range(12):
            (runs / f"20260101-0000{index:02d}.json").write_text("{}\n", encoding="utf-8")
        self.assertError("too many retained run records")

    def test_the_template_runs_folder_ignores_test_records(self) -> None:
        self.assertIn("*.test.json", (ROOT / "bricks/example_brick/runner/runs/.gitignore").read_text(encoding="utf-8"))


class CopyDriftTests(Fixture):
    def marked(self, brick: Path, name: str, version: int, body: str = "x = 1\n") -> Path:
        path = brick / "input" / f"{name}.py"
        path.write_text(f"# BOILERPLATE: {name}@{version}\n{body}", encoding="utf-8")
        return path

    def test_identical_copies_are_clean(self) -> None:
        self.marked(self.brick("a"), "shared", 1)
        self.marked(self.brick("b"), "shared", 1)
        self.assertClean()

    def test_same_version_with_different_content_warns(self) -> None:
        self.marked(self.brick("a"), "shared", 1)
        self.marked(self.brick("b"), "shared", 1, "x = 2\n")
        self.assertTrue(any("copy of 'shared'@1 differs" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_different_versions_name_the_older_copy(self) -> None:
        self.marked(self.brick("a"), "shared", 2)
        self.marked(self.brick("b"), "shared", 1, "x = 2\n")
        self.assertTrue(any("bricks/b/input/shared.py: copy of 'shared' is at @1" in w for w in self.warnings()))

    def test_unmarked_files_are_not_compared(self) -> None:
        (self.brick("a") / "input/loose.py").write_text("x = 1\n", encoding="utf-8")
        (self.brick("b") / "input/loose.py").write_text("x = 2\n", encoding="utf-8")
        self.assertClean()


class DeterminismTests(Fixture):
    def test_template_declares_determinism(self) -> None:
        self.assertIn("DETERMINISTIC = True", (ROOT / "bricks/example_brick/contract.py").read_text(encoding="utf-8"))

    def test_determinism_must_be_a_bool(self) -> None:
        self.contract(self.brick(), DETERMINISTIC='"yes"')
        self.assertError("DETERMINISTIC must be True or False")

    def test_deterministic_src_may_not_use_randomness_threads_or_ids(self) -> None:
        brick = self.brick()
        (brick / "src/helpers.py").write_text("import random\nimport uuid\nfrom threading import Thread\n", encoding="utf-8")
        for name in ("random", "uuid", "threading"):
            self.assertError(f"DETERMINISTIC = True, but src imports {name!r}")

    def test_deterministic_src_may_not_read_the_clock(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "from datetime import datetime\n\n\ndef stamp():\n    return datetime.now()\n", encoding="utf-8"
        )
        self.assertError("DETERMINISTIC = True, but src reads the clock with now()")

    def test_nondeterminism_can_be_declared(self) -> None:
        brick = self.brick()
        self.contract(brick, DETERMINISTIC="False")
        (brick / "src/helpers.py").write_text("import random\nimport time\n", encoding="utf-8")
        self.assertClean()

    def test_saying_nothing_makes_no_promise(self) -> None:
        brick = self.brick()
        path = brick / "contract.py"
        text = path.read_text(encoding="utf-8").replace("DETERMINISTIC = True\n", "")
        path.write_text(text, encoding="utf-8")
        (brick / "src/helpers.py").write_text("import random\n", encoding="utf-8")
        self.assertClean()

    def test_adapters_may_bring_in_the_clock(self) -> None:
        brick = self.brick()
        self.adapter(brick, "clock", "import time\n\n\ndef now(ctx):\n    return ctx['io']\n")
        self.assertFalse([item for item in self.errors() if "DETERMINISTIC" in item])

    def test_a_pure_brick_may_not_declare_nondeterminism(self) -> None:
        brick = self.pure_brick()
        self.contract(brick, DETERMINISTIC="False")
        self.assertError("a pure brick is deterministic by definition")


class PureLaneTests(Fixture):
    def test_pure_brick_lints_clean(self) -> None:
        self.pure_brick()
        self.assertClean()

    def test_pure_brick_needs_no_adapters_folder(self) -> None:
        brick = self.pure_brick()
        shutil.rmtree(brick / "input/adapters")
        self.assertClean()

    def test_a_brick_with_ports_needs_the_adapters_and_tapes_folders(self) -> None:
        brick = self.brick()
        shutil.rmtree(brick / "input/adapters")
        shutil.rmtree(brick / "input/tapes")
        self.assertError("input/adapters: required brick path is missing")
        self.assertError("input/tapes: required brick path is missing")

    def test_a_brick_with_only_joints_needs_no_tapes_folder(self) -> None:
        self.brick("other")
        brick = self.brick()
        (brick / "input/adapters/example_source.py").unlink()
        self.contract(brick, PORTS="{}")
        self.joint(brick, "other")
        shutil.rmtree(brick / "input/tapes")
        self.assertClean()

    def test_pure_brick_without_the_folder_still_may_not_gain_adapters(self) -> None:
        brick = self.pure_brick()
        self.assertTrue((brick / "input/adapters").is_dir())
        self.adapter(brick, "other", "x = 1\n")
        self.assertError("pure brick may not have adapters")

    def test_template_is_not_pure(self) -> None:
        # The template ships one adapter and a filesystem tape loader:
        # both are exactly what a pure brick gives up.
        self.contract(self.brick(), LANE='"pure"')
        self.assertError("pure brick may not have adapters")
        self.assertError("input/tape.py: pure brick imports 'pathlib' outside runner/")

    def test_pure_brick_copied_from_the_template_is_told_to_delete_the_tape_loader(self) -> None:
        self.contract(self.brick(), LANE='"pure"')
        self.assertError("a pure brick has no ports, so delete input/tape.py")

    def test_pure_brick_may_not_declare_sibling_dependencies(self) -> None:
        self.brick("other")
        self.contract(self.pure_brick(), PORTS='{"other": {"kind": "joint", "policy": "eventual", "pin": 1}}')
        self.assertError("pure brick may not declare ports")

    def test_pure_brick_bans_direct_io_outside_runner(self) -> None:
        brick = self.pure_brick()
        (brick / "input/loader.py").write_text("import os\n", encoding="utf-8")
        self.assertError("input/loader.py: pure brick imports 'os' outside runner/")

    def test_pure_brick_reports_src_direct_io_once(self) -> None:
        brick = self.pure_brick()
        (brick / "src/helpers.py").write_text("import os\n", encoding="utf-8")
        hits = [item for item in self.errors() if "src/helpers.py" in item]
        self.assertEqual(hits, ["bricks/example_brick/src/helpers.py: src imports direct-I/O module 'os'"])

    def test_pure_brick_bans_random_and_time_outside_runner(self) -> None:
        brick = self.pure_brick()
        (brick / "src/helpers.py").write_text("import random\nfrom time import sleep\n", encoding="utf-8")
        self.assertError("pure brick imports 'random' outside runner/")
        self.assertError("pure brick imports 'time' outside runner/")

    def test_pure_brick_runner_keeps_its_io(self) -> None:
        brick = self.pure_brick()
        (brick / "runner/rng.py").write_text("import random\nimport pathlib\nimport time\n", encoding="utf-8")
        self.assertClean()

    def test_pure_brick_may_not_read_the_clock(self) -> None:
        brick = self.pure_brick()
        (brick / "src/logic.py").write_text(
            "from datetime import datetime\n\n\ndef stamp():\n    return datetime.now()\n",
            encoding="utf-8",
        )
        self.assertError("src/logic.py: pure brick reads the clock with now()")

    def test_pure_brick_may_use_datetime_arithmetic(self) -> None:
        brick = self.pure_brick()
        (brick / "src/logic.py").write_text(
            "from datetime import datetime, timedelta\n\n\n"
            "def tomorrow(now: datetime) -> datetime:\n    return now + timedelta(days=1)\n",
            encoding="utf-8",
        )
        self.assertClean()

    def test_strict_brick_may_read_the_clock_in_src_when_it_says_so(self) -> None:
        brick = self.brick()
        self.contract(brick, DETERMINISTIC="False")
        (brick / "src/logic.py").write_text(
            "from datetime import datetime\n\n\ndef stamp():\n    return datetime.now()\n",
            encoding="utf-8",
        )
        self.assertClean()


SMOKE = (
    "import os\nimport unittest\nfrom unittest import mock\n\nfrom bricks.example_brick import run\n\n\n"
    "class SmokeTest(unittest.TestCase):\n"
    "    def setUp(self) -> None:\n"
    "        patcher = mock.patch.dict(os.environ, {'BRICK_RUN_KIND': 'test'})\n"
    "        patcher.start()\n"
    "        self.addCleanup(patcher.stop)\n\n"
    "    def test_run(self) -> None:\n"
    "        with self.assertRaises(NotImplementedError):\n"
    "            run({})\n"
)


class WorkflowLaneTests(Fixture):
    def workflow_brick(self, name: str = "example_brick") -> Path:
        brick = self.brick(name)
        self.contract(brick, LANE='"workflow"')
        return brick

    def smoke(self, brick: Path) -> None:
        tests = brick / "runner/tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_smoke.py").write_text(SMOKE, encoding="utf-8")

    def test_workflow_brick_lints_clean(self) -> None:
        self.workflow_brick()
        self.assertClean()

    def test_nothing_may_depend_on_a_workflow(self) -> None:
        self.workflow_brick("flow")
        self.joint(self.brick("lib"), "flow")
        self.assertError("bricks/lib/contract.py: joint 'flow' is a workflow brick")

    def test_workflow_may_have_a_process_door(self) -> None:
        brick = self.workflow_brick()
        (brick / "__main__.py").write_text("from . import run\n\nrun({})\n", encoding="utf-8")
        self.assertClean()

    def test_process_door_may_name_the_brick_in_full(self) -> None:
        brick = self.workflow_brick()
        (brick / "__main__.py").write_text("from bricks.example_brick import run\n\nrun({})\n", encoding="utf-8")
        self.assertClean()

    def test_process_door_may_import_only_run(self) -> None:
        brick = self.workflow_brick()
        (brick / "__main__.py").write_text("from .src.logic import execute\n", encoding="utf-8")
        self.assertError("__main__.py: __main__ may import only this brick's run")

    def test_process_door_may_not_reach_a_sibling(self) -> None:
        self.brick("other")
        brick = self.workflow_brick()
        (brick / "__main__.py").write_text("from bricks.other import run\n", encoding="utf-8")
        self.assertError("__main__.py: __main__ may import only this brick's run")

    def test_only_a_workflow_has_a_process_door(self) -> None:
        brick = self.brick()
        (brick / "__main__.py").write_text("from . import run\n", encoding="utf-8")
        self.assertError("__main__.py: only a workflow brick may have __main__.py")

    def test_smoke_tests_belong_to_workflows(self) -> None:
        self.smoke(self.workflow_brick())
        self.assertClean()

    def test_smoke_test_that_forgets_the_test_kind_warns(self) -> None:
        brick = self.workflow_brick()
        self.smoke(brick)
        text = (brick / "runner/tests/test_smoke.py").read_text(encoding="utf-8")
        (brick / "runner/tests/test_smoke.py").write_text(text.replace("BRICK_RUN_KIND", "OTHER"), encoding="utf-8")
        self.assertTrue(any("does not set BRICK_RUN_KIND=test" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_strict_brick_may_not_carry_smoke_tests(self) -> None:
        # Before lanes this was derived: a brick nothing depended on could carry
        # them. Now the brick says so, or it cannot.
        self.smoke(self.brick())
        self.assertError("runner/tests: smoke tests belong only to workflow bricks")


if __name__ == "__main__":
    unittest.main()
