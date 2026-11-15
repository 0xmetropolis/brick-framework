"""Tests for the reference runner in bricks/example_brick/runner/, which every brick copies."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LOGIC = '''
from ..contract import BrickInput, BrickOutput

CALLS = []


def execute(inputs, run_context):
    CALLS.append(run_context)
    if inputs.get("boom"):
        raise ValueError("boom")
    return {"echo": inputs, "big": "x" * inputs.get("pad", 0)}
'''


class RunnerTests(unittest.TestCase):
    """A copy of the example brick with working logic, imported under a private package name."""

    counter = 0

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        RunnerTests.counter += 1
        self.package = f"runnerfix{RunnerTests.counter}"
        self.brick = Path(tmp.name) / self.package
        shutil.copytree(ROOT / "bricks/example_brick", self.brick, ignore=shutil.ignore_patterns("__pycache__"))
        (self.brick / "src/logic.py").write_text(LOGIC, encoding="utf-8")
        sys.path.insert(0, tmp.name)
        self.addCleanup(sys.path.remove, tmp.name)
        self.addCleanup(self.forget)
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        import os

        os.environ.pop("BRICK_RUN_KIND", None)
        self.module = importlib.import_module(f"{self.package}.runner.run")
        self.logic = importlib.import_module(f"{self.package}.src.logic")

    def forget(self) -> None:
        for name in [name for name in sys.modules if name.split(".")[0] == self.package]:
            del sys.modules[name]

    def records(self, pattern: str = "*.json") -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((self.brick / "runner/runs").glob(pattern))]

    def config(self, **limits: int) -> None:
        path = self.brick / "input/config.yml"
        text = path.read_text(encoding="utf-8")
        for key, value in limits.items():
            lines = [f"  {key}: {value}" if line.strip().startswith(f"{key}:") else line for line in text.splitlines()]
            text = "\n".join(lines) + "\n"
        path.write_text(text, encoding="utf-8")

    def test_run_returns_output_and_records_it(self) -> None:
        output = self.module.run({"day": 1})
        self.assertEqual(output["echo"], {"day": 1})
        (record,) = self.records()
        self.assertEqual((record["kind"], record["mode"], record["input"]), ("run", "saved", {"day": 1}))
        self.assertEqual(record["output"]["echo"], {"day": 1})
        self.assertRegex(record["run_id"], r"^\d{8}-\d{6}-\d{6}-[0-9a-f]{4}$")

    def test_the_context_has_run_id_mode_and_nested_config(self) -> None:
        self.module.run({}, fresh=True)
        context = self.logic.CALLS[-1]
        self.assertEqual(context["mode"], "fresh")
        self.assertEqual(context["config"]["limits"]["max_evidence_bytes"], 122880)
        self.assertEqual(context["config"]["retention"]["runs"], 10)
        self.assertEqual(context["config"]["brick"], "example_brick")

    def test_save_implies_fresh_and_is_the_save_mode(self) -> None:
        self.module.run({}, save=True)
        self.assertEqual(self.logic.CALLS[-1]["mode"], "save")

    def test_a_failure_is_recorded_and_raised(self) -> None:
        with self.assertRaises(ValueError):
            self.module.run({"boom": True})
        (record,) = self.records()
        self.assertEqual(record["error"], {"type": "ValueError", "message": "boom"})
        self.assertNotIn("output", record)

    def test_old_records_are_pruned_to_the_retention(self) -> None:
        self.config(runs=3)
        for index in range(5):
            self.module.run({"i": index})
        self.assertEqual([record["input"]["i"] for record in self.records()], [2, 3, 4])

    def test_test_runs_are_local_and_bounded_on_their_own(self) -> None:
        self.config(runs=2)
        self.module.run({"i": "tracked"})
        with mock.patch.dict("os.environ", {"BRICK_RUN_KIND": "test"}):
            for index in range(4):
                self.module.run({"i": index})
        self.assertEqual([record["input"]["i"] for record in self.records("*.test.json")], [2, 3])
        self.assertEqual([record["input"]["i"] for record in self.records("[0-9]*[0-9a-f].json")], ["tracked"])
        self.assertEqual(self.records("*.test.json")[0]["kind"], "test")

    def test_the_runs_folder_ignores_test_records(self) -> None:
        self.assertIn("*.test.json", (self.brick / "runner/runs/.gitignore").read_text(encoding="utf-8"))

    def test_an_oversize_record_drops_output_then_input(self) -> None:
        self.config(max_run_record_bytes=1000)
        self.module.run({"pad": 5000})
        (record,) = self.records()
        self.assertIn("omitted_bytes", record["output"])
        self.assertEqual(record["input"], {"pad": 5000})
        self.module.run({"pad": 1, "more": "y" * 3000})
        self.assertIn("omitted_bytes", self.records()[-1]["input"])

    def test_config_reader_handles_the_subset(self) -> None:
        path = self.brick / "input/config.yml"
        path.write_text(
            "a: 1  # note\nb:\n  c: true\n  d: null\n  e: \"x y\"\n  f:\n    g: -4\nname: plain text\n",
            encoding="utf-8",
        )
        self.assertEqual(
            self.module.load_config(path),
            {"a": 1, "b": {"c": True, "d": None, "e": "x y", "f": {"g": -4}}, "name": "plain text"},
        )

    def test_config_reader_names_the_bad_line(self) -> None:
        path = self.brick / "input/config.yml"
        path.write_text("ok: 1\nnot a mapping line\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self.module.load_config(path)
        self.assertIn("config.yml:2", str(caught.exception))

    def test_the_committed_config_parses(self) -> None:
        config = self.module.load_config(ROOT / "bricks/example_brick/input/config.yml")
        self.assertEqual(config["limits"]["max_run_record_bytes"], 122880)


if __name__ == "__main__":
    unittest.main()
