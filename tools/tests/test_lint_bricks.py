"""Tests for tools/lint_bricks.py, standard library only.

Every test builds a throwaway repository from the committed boilerplate,
mutates one thing, and asserts on the exact message the linter reports.
The boilerplate itself must lint clean; that is the first test.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from tools import lint_bricks

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
        """Rewrite one or more top-level assignments in the brick's contract."""
        path = brick / "contract.py"
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for name, value in values.items():
            for index, line in enumerate(lines):
                if line.startswith(f"{name}:") or line.startswith(f"{name} ="):
                    lines[index] = f"{name} = {value}\n"
                    break
            else:
                lines.append(f"{name} = {value}\n")
        path.write_text("".join(lines), encoding="utf-8")


    def pure_brick(self, name: str = "example_brick") -> Path:
        """The template with its one adapter and the evidence loader removed."""
        brick = self.brick(name)
        (brick / "input/adapters/example_source.py").unlink()
        (brick / "input/evidence.py").unlink()
        self.contract(brick, LANE='"pure"')
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
    def test_src_reading_the_mode_warns_and_does_not_fail(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def decide(ctx):\n    return ctx['mode'] == 'saved' or ctx.get('mode')\n", encoding="utf-8"
        )
        warnings = [item for item in self.warnings() if "src/logic.py: src reads the run mode" in item]
        self.assertEqual(len(warnings), 2)
        self.assertEqual(self.errors(), [])

    def test_adapter_reading_the_mode_is_its_job(self) -> None:
        self.brick()
        self.assertClean()

    def test_src_reading_a_mode_field_of_its_input_does_not_warn(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def decide(inputs, ctx):\n    return inputs['mode'], ctx['config'], ctx['run_id']\n",
            encoding="utf-8",
        )
        self.assertClean()


SIBLING_ADAPTER = (
    "from bricks.other import run\n\n\ndef fetch(request, ctx):\n    return run(request)\n"
)
ADAPTER_WITH_SAVED_MODE = (
    "from ..evidence import load_example\n\n\n"
    "def fetch(request, ctx):\n{body}    return load_example('feed', 'default', request)['response']\n"
)


class ModeBlindFunctionTests(Fixture):
    ADAPTER = (
        "from pathlib import Path\nfrom ..evidence import load_example\n\n\n"
        "def exists(template_dir, seed):\n    return (Path(template_dir) / f'seed-{seed}.save').is_file()\n\n\n"
        "def store(ctx, template_dir, seed):\n    return load_example('t', 'c', {})\n\n\n"
        "def stats(pid):\n    return Path(f'/proc/{pid}').exists()\n"
    )

    def test_io_function_without_context_called_from_src_warns(self) -> None:
        brick = self.brick()
        (brick / "input/adapters/template_files.py").write_text(self.ADAPTER, encoding="utf-8")
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
        (brick / "input/adapters/template_files.py").write_text(self.ADAPTER, encoding="utf-8")
        self.assertFalse([w for w in self.warnings() if "takes no run context" in w])

    def test_importing_the_function_by_name_counts_as_a_call_from_outside(self) -> None:
        brick = self.brick()
        (brick / "input/adapters/template_files.py").write_text(self.ADAPTER, encoding="utf-8")
        (brick / "src/logic.py").write_text(
            "from ..input.adapters.template_files import exists\n\n\ndef execute(inputs, ctx):\n    return exists('d', 1)\n",
            encoding="utf-8",
        )
        self.assertTrue(any("exists() does I/O" in w for w in self.warnings()))

    def test_function_that_takes_the_context_is_fine(self) -> None:
        brick = self.brick()
        (brick / "input/adapters/template_files.py").write_text(
            self.ADAPTER.replace("def exists(template_dir, seed)", "def exists(ctx, template_dir, seed)"), encoding="utf-8"
        )
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
        (brick / "input/adapters/dyn.py").write_text(
            "import importlib\nfrom ..evidence import load_example\n\n\ndef f(ctx):\n    load_example('a', 'b', {})\n    return importlib.import_module('bricks.other')\n",
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
        (brick / "input/loader.py").write_text("def f(x):\n    return __import__(x)\n", encoding="utf-8")
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
    def adapter(self, brick: Path, name: str, text: str) -> None:
        (brick / "input/adapters" / f"{name}.py").write_text(text, encoding="utf-8")

    def test_env_switch_in_an_adapter_warns(self) -> None:
        brick = self.brick()
        self.adapter(
            brick, "feed",
            ADAPTER_WITH_SAVED_MODE.format(body="    if os.environ.get('SYX_FEED_REPLAY'):\n        pass\n")
            .replace("from ..evidence", "import os\nfrom ..evidence"),
        )
        self.assertTrue(any("environment variable 'SYX_FEED_REPLAY'" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_getenv_and_subscript_forms_warn(self) -> None:
        brick = self.brick()
        self.adapter(
            brick, "feed",
            "import os\nfrom ..evidence import load_example\n\n\ndef fetch(request, ctx):\n"
            "    a = os.getenv('FEED_LIVE')\n    b = os.environ['FEED_OFFLINE']\n"
            "    return load_example('feed', 'default', request)\n",
        )
        hits = [w for w in self.warnings() if "environment variable" in w]
        self.assertEqual(len(hits), 2)

    def test_credentials_and_paths_in_the_environment_are_fine(self) -> None:
        brick = self.brick()
        self.adapter(
            brick, "feed",
            "import os\nfrom ..evidence import load_example\n\n\ndef fetch(request, ctx):\n"
            "    token = os.environ.get('FEED_TOKEN')\n    root = os.getenv('FEED_ROOT')\n"
            "    return load_example('feed', 'default', request)\n",
        )
        self.assertTrue(all("environment variable" not in w for w in self.warnings()))

    def test_adapter_with_no_saved_mode_warns(self) -> None:
        brick = self.brick()
        self.adapter(brick, "tool", "import subprocess\n\n\ndef encode(request, ctx):\n    return {}\n")
        self.assertTrue(any("tool.py: adapter never calls load_example" in w for w in self.warnings()))

    def test_sibling_adapter_needs_no_saved_mode_of_its_own(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.contract(brick, SIBLING_DEPENDENCIES='{"other": "eventual"}')
        self.adapter(brick, "other", SIBLING_ADAPTER)
        self.assertTrue(all("never calls load_example" not in w for w in self.warnings()))

    def test_src_overriding_the_mode_warns(self) -> None:
        brick = self.brick()
        (brick / "src/logic.py").write_text(
            "def second(ctx):\n    return dict(ctx, mode='fresh')\n", encoding="utf-8"
        )
        self.assertTrue(any("src overrides the run mode" in w for w in self.warnings()))

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

    def test_forwarding_fresh_to_a_sibling_explains_what_to_do(self) -> None:
        self.brick("other")
        brick = self.brick()
        self.contract(brick, SIBLING_DEPENDENCIES='{"other": "eventual"}')
        self.adapter(
            brick, "other",
            "from bricks.other import run\n\n\ndef fetch(request, ctx):\n    return run(request, fresh=True)\n",
        )
        self.assertError("a sibling always replays")
        self.assertError("Sources several bricks share")


class EvidenceSizeTests(Fixture):
    def evidence(self, brick: Path, case: str, padding: int) -> Path:
        import json

        record = {
            "adapter": "feed", "capture_run_id": "r1", "case": case, "contract_version": 1,
            "request": {}, "response": {"pad": "x" * padding}, "schema_version": 1,
        }
        folder = brick / "input/data/feed"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{case}.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def test_evidence_near_the_limit_warns(self) -> None:
        brick = self.brick()
        self.evidence(brick, "big", 105_000)
        self.assertTrue(any("big.json: evidence is 8" in w and "% of max_evidence_bytes" in w for w in self.warnings()))
        self.assertEqual(self.errors(), [])

    def test_small_evidence_is_quiet(self) -> None:
        brick = self.brick()
        self.evidence(brick, "small", 1_000)
        self.assertClean()

    def test_evidence_over_the_limit_is_an_error_with_a_hint(self) -> None:
        brick = self.brick()
        self.evidence(brick, "huge", 130_000)
        self.assertError("evidence exceeds max_evidence_bytes; store a projection or split it into cases")

    def test_too_many_cases_says_it_is_a_deliberate_choice(self) -> None:
        brick = self.brick()
        for index in range(4):
            self.evidence(brick, f"w{index}", 10)
        self.assertError("raise retention.saved_examples_per_adapter in config.yml deliberately")


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
        (brick / "input/adapters/clock.py").write_text(
            "import time\n\n\ndef now(ctx):\n    return ctx['io']\n", encoding="utf-8"
        )
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

    def test_strict_brick_still_needs_adapters_folder(self) -> None:
        brick = self.pure_brick()
        self.contract(brick, LANE='"strict"')
        shutil.rmtree(brick / "input/adapters")
        self.assertError("input/adapters: required brick path is missing")

    def test_workflow_brick_still_needs_adapters_folder(self) -> None:
        brick = self.pure_brick()
        self.contract(brick, LANE='"workflow"')
        shutil.rmtree(brick / "input/adapters")
        self.assertError("input/adapters: required brick path is missing")

    def test_pure_brick_without_the_folder_still_may_not_gain_adapters(self) -> None:
        brick = self.pure_brick()
        self.assertTrue((brick / "input/adapters").is_dir())
        (brick / "input/adapters/other.py").write_text("x = 1\n", encoding="utf-8")
        self.assertError("pure brick may not have adapters")

    def test_template_is_not_pure(self) -> None:
        # The template ships one adapter and a filesystem evidence loader:
        # both are exactly what a pure brick gives up.
        self.contract(self.brick(), LANE='"pure"')
        self.assertError("pure brick may not have adapters")
        self.assertError("input/evidence.py: pure brick imports 'pathlib' outside runner/")

    def test_pure_brick_copied_from_the_template_is_told_to_delete_evidence(self) -> None:
        self.contract(self.brick(), LANE='"pure"')
        self.assertError("a pure brick has no saved evidence, so delete input/evidence.py")

    def test_pure_brick_may_not_declare_sibling_dependencies(self) -> None:
        self.brick("other")
        self.contract(self.pure_brick(), SIBLING_DEPENDENCIES='{"other": "eventual"}')
        self.assertError("pure brick may not declare sibling dependencies")

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
        self.contract(self.brick("lib"), SIBLING_DEPENDENCIES='{"flow": "eventual"}')
        self.assertError("bricks/lib/contract.py: sibling dependency 'flow' is a workflow brick")

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
