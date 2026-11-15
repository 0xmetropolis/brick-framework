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
        self.assertEqual(
            (record["kind"], record["fresh"], record["record"], record["followed"], record["input"]),
            ("run", [], [], [], {"day": 1}),
        )
        self.assertNotIn("mode", record)
        self.assertEqual(record["output"]["echo"], {"day": 1})
        self.assertRegex(record["run_id"], r"^\d{8}-\d{6}-\d{6}-[0-9a-f]{4}$")

    def test_the_context_has_run_id_plan_and_nested_config(self) -> None:
        self.module.run({}, fresh=True)
        context = self.logic.CALLS[-1]
        self.assertEqual(context["plan"], {"fresh": ("example_source",), "record": ()})
        self.assertNotIn("mode", context)
        self.assertEqual(context["config"]["limits"]["max_take_bytes"], 122880)
        self.assertEqual(context["config"]["retention"]["runs"], 10)
        self.assertEqual(context["config"]["brick"], "example_brick")

    def test_record_implies_fresh_for_the_same_names(self) -> None:
        self.module.run({}, record=True)
        self.assertEqual(self.logic.CALLS[-1]["plan"], {"fresh": ("example_source",), "record": ("example_source",)})
        (record,) = self.records()
        self.assertEqual((record["fresh"], record["record"]), (["example_source"], ["example_source"]))

    def test_the_old_save_option_is_gone(self) -> None:
        with self.assertRaises(TypeError):
            self.module.run({}, save=True)

    def test_nothing_is_fresh_by_default(self) -> None:
        self.module.run({})
        self.assertEqual(self.logic.CALLS[-1]["plan"], {"fresh": (), "record": ()})

    def plan(self, fresh=False, record=False, ports=None):
        ports = ports if ports is not None else {
            "rates": {"kind": "source", "epoch": 1},
            "game": {"kind": "source", "epoch": 1},
            "model": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("xai", "tts")},
            "other": {"kind": "joint", "policy": "eventual", "pin": 1},
        }
        with mock.patch.object(self.module, "PORTS", ports):
            return self.module.make_plan(fresh, record)

    def test_true_means_this_bricks_own_sources_and_never_a_sibling(self) -> None:
        self.assertEqual(self.plan(fresh=True), {"fresh": ("game", "rates"), "record": ()})

    def test_names_pick_individual_sources(self) -> None:
        self.assertEqual(self.plan(fresh={"game"}), {"fresh": ("game",), "record": ()})
        self.assertEqual(self.plan(fresh=["rates"], record="game"), {"fresh": ("game", "rates"), "record": ("game",)})

    def test_a_dotted_name_reaches_a_followed_source(self) -> None:
        self.assertEqual(self.plan(fresh={"model.xai"}), {"fresh": ("model.xai",), "record": ()})
        self.assertEqual(self.plan(record={"model.tts"}), {"fresh": ("model.tts",), "record": ("model.tts",)})

    def test_star_means_every_own_source_and_every_followed_one(self) -> None:
        self.assertEqual(
            self.plan(fresh={"*"}),
            {"fresh": ("game", "model.tts", "model.xai", "rates"), "record": ()},
        )

    def test_an_unknown_or_unfollowed_name_is_an_error_that_lists_what_exists(self) -> None:
        for name in ("nothing", "model", "other.xai", "model.nothing", "xai"):
            with self.assertRaises(ValueError) as caught:
                self.plan(fresh={name})
            self.assertIn(f"fresh= names {name!r}", str(caught.exception))
            self.assertIn("known: game, model.tts, model.xai, rates", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            self.plan(record={"nothing"})
        self.assertIn("record= names 'nothing'", str(caught.exception))

    def test_a_brick_with_no_sources_has_nothing_to_name(self) -> None:
        self.assertEqual(self.plan(fresh=True, ports={}), {"fresh": (), "record": ()})
        with self.assertRaises(ValueError) as caught:
            self.plan(fresh={"x"}, ports={})
        self.assertIn("known: none", str(caught.exception))

    def test_followed_names_are_listed_apart_from_own_ones_in_the_record(self) -> None:
        ports = {"rates": {"kind": "source", "epoch": 1}, "model": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("xai",)}}
        with mock.patch.object(self.module, "PORTS", ports):
            self.module.run({}, fresh={"rates", "model.xai"}, record={"rates"})
        (record,) = self.records()
        self.assertEqual((record["fresh"], record["record"], record["followed"]), (["rates"], ["rates"], ["model.xai"]))

    def test_the_run_closes_what_the_context_registered_even_when_it_fails(self) -> None:
        closed = []

        def execute(inputs, context):
            context["closers"].append(lambda failed: closed.append(("a", failed)))
            context["closers"].append(lambda failed: closed.append(("b", failed)))
            if inputs.get("boom"):
                raise ValueError("boom")
            return {}

        self.module.execute = execute
        self.module.run({})
        with self.assertRaises(ValueError):
            self.module.run({"boom": True})
        self.assertEqual(closed, [("b", False), ("a", False), ("b", True), ("a", True)])

    def test_a_failing_close_is_reported_and_does_not_stop_the_others(self) -> None:
        closed = []

        def execute(inputs, context):
            context["closers"].append(lambda failed: closed.append("first"))

            def bad(failed):
                raise OSError("disk full")

            context["closers"].append(bad)
            return {}

        self.module.execute = execute
        with self.assertRaises(OSError):
            self.module.run({})
        self.assertEqual(closed, ["first"])
        (record,) = self.records()
        self.assertEqual(record["error"], {"type": "OSError", "message": "disk full"})

    def test_a_failure_hides_nothing_when_a_close_fails_too(self) -> None:
        def execute(inputs, context):
            def bad(failed):
                raise OSError("disk full")

            context["closers"].append(bad)
            raise ValueError("the real one")

        self.module.execute = execute
        with self.assertRaises(ValueError):
            self.module.run({})

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

    def test_concurrent_runs_all_return_their_output(self) -> None:
        import threading

        self.config(runs=3)
        errors: list[str] = []

        def work() -> None:
            for index in range(40):
                try:
                    self.module.run({"i": index})
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(self.records()), 1)

    def test_the_runs_folder_ignores_test_records(self) -> None:
        self.assertIn("*.test.json", (self.brick / "runner/runs/.gitignore").read_text(encoding="utf-8"))

    def test_an_oversize_record_keeps_its_shape_and_says_what_it_summarized(self) -> None:
        self.config(max_run_record_bytes=2000)
        self.module.run({"pad": 5000})
        (record,) = self.records()
        self.assertEqual(record["input"], {"pad": 5000})
        self.assertEqual(record["output"]["big"]["chars"], 5000)
        self.assertEqual(len(record["output"]["big"]["sha256"]), 16)
        self.assertEqual(record["output"]["echo"], {"pad": 5000})
        self.assertEqual(record["summarized"], ["output"])
        self.assertLessEqual((self.brick / "runner/runs" / f"{record['run_id']}.json").stat().st_size, 2000)

    def test_bytes_are_recorded_as_size_and_digest_not_repr(self) -> None:
        self.config(max_run_record_bytes=2000)
        self.module.run({"frames": {"d1.png": b"\x89PNG" * 1000}, "keep": 1})
        (record,) = self.records()
        frame = record["input"]["frames"]["d1.png"]
        self.assertEqual(frame["bytes"], 4000)
        self.assertEqual(len(frame["sha256"]), 16)
        self.assertEqual(record["input"]["keep"], 1)
        self.assertNotIn("PNG", json.dumps(record))

    def test_a_workflow_sized_output_still_names_its_files(self) -> None:
        self.config(max_run_record_bytes=4000)
        files = {f"run-1/file-{index}.txt": "line\n" * 20_000 for index in range(17)}
        with mock.patch.object(self.logic, "execute", lambda inputs, ctx: {"passed": True, "artifacts": files}):
            self.module.execute = self.logic.execute
            self.module.run({})
        (record,) = self.records()
        self.assertEqual(sorted(record["output"]["artifacts"]), sorted(files))
        self.assertEqual(record["output"]["artifacts"]["run-1/file-3.txt"]["chars"], 100_000)
        self.assertTrue(record["output"]["passed"])

    def test_a_record_too_big_even_summarized_falls_back_to_keys_and_size(self) -> None:
        self.config(max_run_record_bytes=600)
        wide = {f"key-{index}": index for index in range(400)}
        self.module.run({"wide": wide})
        (record,) = self.records()
        self.assertIn("omitted_bytes", record["input"])
        self.assertIn("input", record["summarized"])

    def test_a_small_record_is_not_marked_summarized(self) -> None:
        self.module.run({"day": 1})
        (record,) = self.records()
        self.assertNotIn("summarized", record)

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
