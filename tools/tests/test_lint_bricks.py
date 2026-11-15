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
    "import unittest\n\nfrom bricks.example_brick import run\n\n\n"
    "class SmokeTest(unittest.TestCase):\n"
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

    def test_strict_brick_may_not_carry_smoke_tests(self) -> None:
        # Before lanes this was derived: a brick nothing depended on could carry
        # them. Now the brick says so, or it cannot.
        self.smoke(self.brick())
        self.assertError("runner/tests: smoke tests belong only to workflow bricks")


if __name__ == "__main__":
    unittest.main()
