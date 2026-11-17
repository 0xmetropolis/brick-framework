"""The committed template brick: its recorded take replays and the brick itself lints clean.

The template is checked on its own: a copy of it goes into a temporary project, so only its own
findings count. Warnings or errors in the other bricks of a project are that project's business and
are what ``python3 tools/lint_bricks.py`` is for. The tests are skipped when a project has deleted
the example brick.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools import lint_bricks

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "bricks/example_brick"
# The tests of the template itself, run in a copy of the project without running these project-level ones again.
LEAF = "tools.tests.test_example_brick.ExampleBrickTests"
LEAF_TESTS = (
    "test_the_committed_take_is_schema_two_for_the_templates_port",
    "test_the_committed_take_replays_through_the_templates_adapter",
    "test_the_example_brick_lints_clean_and_its_take_is_current_for_the_stale_tape_lint",
)


def lint_example_alone(root: Path) -> lint_bricks.Lint:
    """Lint a copy of ``root``'s example brick in an otherwise empty project."""
    with tempfile.TemporaryDirectory(prefix="example-brick-") as tmp:
        alone = Path(tmp)
        shutil.copy(root / "AGENTS.md", alone / "AGENTS.md")
        (alone / "bricks").mkdir()
        for name in ("AGENTS.md", "__init__.py"):
            shutil.copy(root / "bricks" / name, alone / "bricks" / name)
        shutil.copytree(root / "bricks/example_brick", alone / "bricks/example_brick", ignore=shutil.ignore_patterns("__pycache__"))
        return lint_bricks.lint_repo(alone)


def copy_project(destination: Path) -> None:
    shutil.copytree(ROOT, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", "reports"))


@unittest.skipUnless(EXAMPLE.is_dir(), "this project has no bricks/example_brick")
class ExampleBrickTests(unittest.TestCase):
    def test_the_committed_take_is_schema_two_for_the_templates_port(self) -> None:
        record = json.loads((EXAMPLE / "input/tapes/example_source/default.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (record["schema_version"], record["source"], record["take"], record["epoch"]),
            (2, "example_source", "default", 1),
        )
        self.assertNotIn("contract_version", record)

    def test_the_committed_take_replays_through_the_templates_adapter(self) -> None:
        from bricks.example_brick.input import tape
        from bricks.example_brick.input.adapters import example_source

        context = {"run_id": "run-1", "config": {}, "plan": {"fresh": (), "record": ()}}
        self.assertEqual(example_source.fetch({"purpose": "example"}, context), {"value": 1})
        with self.assertRaises(tape.TapeError):
            example_source.fetch({"purpose": "other"}, context)

    def test_the_example_brick_lints_clean_and_its_take_is_current_for_the_stale_tape_lint(self) -> None:
        lint = lint_example_alone(ROOT)
        self.assertEqual((lint.errors, lint.warnings), ([], []))

    def test_only_the_example_bricks_own_findings_count(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-brick-project-") as tmp:
            project = Path(tmp) / "project"
            copy_project(project)
            noisy = project / "bricks/noisy"
            shutil.copytree(project / "bricks/example_brick", noisy)
            contract = noisy / "contract.py"
            contract.write_text(
                contract.read_text(encoding="utf-8").replace(
                    'class BrickInput(TypedDict):\n    """Input accepted by run(). Replace with domain fields."""',
                    "class BrickInput(TypedDict):\n    op: str",
                ),
                encoding="utf-8",
            )
            whole = lint_bricks.lint_repo(project)
            self.assertTrue(any("noisy/contract.py: BrickInput.op selects an operation" in w for w in whole.warnings), whole.warnings)
            alone = lint_example_alone(project)
            self.assertEqual((alone.errors, alone.warnings), ([], []))

    def test_the_tests_pass_in_a_project_that_has_an_unrelated_warning(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-brick-project-") as tmp:
            project = Path(tmp) / "project"
            copy_project(project)
            noisy = project / "bricks/noisy"
            shutil.copytree(project / "bricks/example_brick", noisy)
            contract = noisy / "contract.py"
            contract.write_text(contract.read_text(encoding="utf-8").replace("    \"\"\"Input accepted by run(). Replace with domain fields.\"\"\"", "    op: str", 1), encoding="utf-8")
            done = subprocess.run(
                [sys.executable, "-m", "unittest", *(f"{LEAF}.{name}" for name in LEAF_TESTS)],
                cwd=project, capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("Ran 3 tests", done.stderr)

    def test_the_tests_are_skipped_not_broken_in_a_project_without_the_example_brick(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-brick-project-") as tmp:
            project = Path(tmp) / "project"
            copy_project(project)
            shutil.rmtree(project / "bricks/example_brick")
            done = subprocess.run(
                [sys.executable, "-m", "unittest", "tools.tests.test_example_brick", "-v"],
                cwd=project, capture_output=True, text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("skipped 'this project has no bricks/example_brick'", done.stderr)


if __name__ == "__main__":
    unittest.main()
