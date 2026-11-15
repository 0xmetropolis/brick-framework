"""The deterministic half of the review gate: routing, memory, exit rules.

The model half is exercised only in CI. Everything here runs without the
`anthropic` package and without a network.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import review

ROOT = Path(__file__).resolve().parents[2]


class Fixture(unittest.TestCase):
    """A copy of the boilerplate under a temporary root, one brick per name."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="brick-review-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        shutil.copy(ROOT / "AGENTS.md", self.root / "AGENTS.md")
        (self.root / "bricks").mkdir()
        shutil.copy(ROOT / "bricks/AGENTS.md", self.root / "bricks/AGENTS.md")

    def brick(self, name: str) -> Path:
        target = self.root / "bricks" / name
        shutil.copytree(ROOT / "bricks/example_brick", target,
                        ignore=shutil.ignore_patterns("__pycache__"))
        return target

    def sealed(self, name: str) -> Path:
        brick = self.brick(name)
        with (brick / "contract.py").open("a", encoding="utf-8") as handle:
            handle.write("PORTS = {}\n")
        return brick

    def entry(self, name: str) -> Path:
        brick = self.brick(name)
        (brick / "__main__.py").write_text("from . import run\n", encoding="utf-8")
        return brick


class RoutingTests(Fixture):
    def test_brick_of(self) -> None:
        self.assertEqual(review.brick_of("bricks/alpha/src/logic.py"), "alpha")
        self.assertEqual(review.brick_of("bricks/alpha/contract.py"), "alpha")
        self.assertIsNone(review.brick_of("bricks/__init__.py"))
        self.assertIsNone(review.brick_of("bricks/AGENTS.md"))
        self.assertIsNone(review.brick_of("tools/review.py"))
        self.assertIsNone(review.brick_of("README.md"))

    def test_shape_is_computed_from_the_ports_and_the_files(self) -> None:
        self.brick("ordinary")
        self.sealed("calc")
        self.entry("flow")
        self.assertEqual(review.shape_of(self.root, "ordinary"), "open")
        self.assertEqual(review.shape_of(self.root, "calc"), "sealed")
        self.assertEqual(review.shape_of(self.root, "flow"), "entry")

    def test_a_main_makes_a_brick_an_entry_brick_even_with_no_ports(self) -> None:
        brick = self.sealed("both")
        (brick / "__main__.py").write_text("from . import run\n", encoding="utf-8")
        self.assertEqual(review.shape_of(self.root, "both"), "entry")

    def test_missing_or_broken_contract_reads_as_open(self) -> None:
        self.assertEqual(review.shape_of(self.root, "gone"), "open")
        (self.brick("broken") / "contract.py").write_text("PORTS = (", encoding="utf-8")
        (self.brick("dyn") / "contract.py").write_text("PORTS = make()\n", encoding="utf-8")
        for name in ("broken", "dyn"):
            self.assertEqual(review.shape_of(self.root, name), "open", name)

    def test_the_old_lane_declaration_changes_nothing(self) -> None:
        brick = self.brick("old")
        with (brick / "contract.py").open("a", encoding="utf-8") as handle:
            handle.write('LANE = "pure"\n')
        self.assertEqual(review.shape_of(self.root, "old"), "open")

    def test_shapes_are_a_fixed_triple_open_first(self) -> None:
        self.assertEqual(review.SHAPES, ("open", "sealed", "entry"))

    def test_a_repository_overrides_a_prompt_but_cannot_add_a_shape(self) -> None:
        (self.root / "review").mkdir()
        (self.root / "review/money.md").write_text("# money\n", encoding="utf-8")
        (self.root / "review/sealed.md").write_text("# our sealed\n", encoding="utf-8")
        self.brick("desk")
        self.assertEqual(review.SHAPES, ("open", "sealed", "entry"))
        self.assertEqual(list(review.route(self.root, ["bricks/desk/src/logic.py"])[0]), ["open", "sealed", "entry"])
        # Its own prompt where it has one, the framework's where it does not.
        self.assertEqual(review.prompt_path(self.root, "sealed"), self.root / "review/sealed.md")
        self.assertEqual(review.prompt_path(self.root, "open"),
                         review.FRAMEWORK / "review/open.md")
        self.assertEqual(review.prompt_path(self.root, "common"),
                         review.FRAMEWORK / "review/common.md")

    def test_route_splits_by_shape_and_names_the_rest(self) -> None:
        self.brick("ordinary")
        self.sealed("calc")
        shapes, unrouted = review.route(self.root, [
            "bricks/ordinary/src/logic.py",
            "bricks/calc/src/logic.py",
            "bricks/calc/contract.py",
            "tools/lint_bricks.py",
            "bricks/AGENTS.md",
        ])
        self.assertEqual(list(shapes), list(review.SHAPES))
        self.assertEqual(shapes["open"], ["bricks/ordinary/src/logic.py"])
        self.assertEqual(shapes["sealed"], ["bricks/calc/src/logic.py", "bricks/calc/contract.py"])
        self.assertEqual(shapes["entry"], [])
        self.assertEqual(unrouted, ["tools/lint_bricks.py", "bricks/AGENTS.md"])

    def test_brick_docs_follow_the_touched_bricks(self) -> None:
        self.brick("ordinary")
        self.brick("other")
        docs = review.brick_docs(self.root, ["bricks/ordinary/src/logic.py", "bricks/ordinary/contract.py"])
        self.assertEqual(
            [d.relative_to(self.root).as_posix() for d in docs],
            ["bricks/ordinary/input/AGENTS.md", "bricks/ordinary/runner/AGENTS.md", "bricks/ordinary/src/AGENTS.md"],
        )


class PromptTests(unittest.TestCase):
    def test_every_shape_has_a_prompt(self) -> None:
        self.assertTrue((review.FRAMEWORK / review.PROMPTS / review.COMMON_PROMPT).is_file())
        for shape in review.SHAPES:
            self.assertTrue(review.prompt_path(ROOT, shape).is_file(), shape)

    def test_the_prompts_are_exactly_the_shapes_and_common(self) -> None:
        names = sorted(path.name for path in (review.FRAMEWORK / review.PROMPTS).glob("*.md"))
        self.assertEqual(names, ["common.md", "entry.md", "open.md", "sealed.md"])

    def test_context_docs_exist(self) -> None:
        for doc in review.CONTEXT_DOCS:
            self.assertTrue((ROOT / doc).is_file(), doc)


class VerdictTests(unittest.TestCase):
    def test_blocks_only_on_a_block_finding(self) -> None:
        advisory = {"file": "x", "issue": "i", "suggestion": "s", "severity": "advisory"}
        block = dict(advisory, severity="block")
        self.assertFalse(review.blocks({"findings": []}))
        self.assertFalse(review.blocks({"findings": [advisory]}))
        self.assertTrue(review.blocks({"findings": [advisory, block]}))


class EnvironmentTests(unittest.TestCase):
    def test_local_run_needs_nothing(self) -> None:
        self.assertIsNone(review.ci_misconfigured({}))

    def test_one_sha_is_an_error_anywhere(self) -> None:
        self.assertIn("both", review.ci_misconfigured({"BASE_SHA": "a"}))

    def test_ci_needs_both_shas(self) -> None:
        self.assertIn("CI", review.ci_misconfigured({"GITHUB_ACTIONS": "true"}))

    def test_ci_needs_a_key(self) -> None:
        message = review.ci_misconfigured(
            {"GITHUB_ACTIONS": "true", "BASE_SHA": "a", "HEAD_SHA": "b"})
        self.assertIn("ANTHROPIC_API_KEY", message)
        self.assertIn("not a review", message)

    def test_ci_fully_configured(self) -> None:
        self.assertIsNone(review.ci_misconfigured(
            {"GITHUB_ACTIONS": "true", "BASE_SHA": "a", "HEAD_SHA": "b",
             "ANTHROPIC_API_KEY": "k"}))

    def test_diff_range(self) -> None:
        self.assertEqual(review.diff_range("a", "b"), ["a...b"])
        self.assertEqual(review.diff_range(None, None), ["HEAD"])


class MemoryTests(Fixture):
    """Carry-forward on a real, tiny repository."""

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def setUp(self) -> None:
        super().setUp()
        self.brick("ordinary")
        self.git("init", "-q")
        self.first = self.commit("start")

    def test_no_memory_means_full_review(self) -> None:
        self.assertFalse(review.carried_forward(self.root, {}, self.first))

    def test_same_head_is_not_carried(self) -> None:
        previous = {"head": self.first, "shapes": {"open": {"findings": []}}}
        self.assertFalse(review.carried_forward(self.root, previous, self.first))

    def test_docs_only_delta_carries_forward(self) -> None:
        previous = {"head": self.first, "shapes": {"open": {"findings": []}}}
        (self.root / "README.md").write_text("notes\n", encoding="utf-8")
        head = self.commit("docs")
        self.assertTrue(review.carried_forward(self.root, previous, head))

    def test_brick_delta_reviews_again(self) -> None:
        previous = {"head": self.first, "shapes": {"open": {"findings": []}}}
        (self.root / "bricks/ordinary/src/logic.py").write_text("X = 1\n", encoding="utf-8")
        head = self.commit("logic")
        self.assertFalse(review.carried_forward(self.root, previous, head))

    def test_unknown_sha_reviews_again(self) -> None:
        previous = {"head": "0" * 40, "shapes": {"open": {"findings": []}}}
        self.assertFalse(review.carried_forward(self.root, previous, self.first))
