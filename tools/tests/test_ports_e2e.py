"""Source ports, sessions and following end to end, on a fake live source, in a throwaway repository.

``game`` owns a followable source port: it drives a source over a session, records it on tape
and replays by default. ``flow`` reaches it through a joint that follows that source. The fake
source stands in for what a launcher would have started.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools import lint_bricks

ROOT = Path(__file__).resolve().parents[2]

SOURCE_ADAPTER = '''
import os

from ..tape import session


class FakeSource:
    """What a launcher would have started: reports its version and answers calls."""

    def __init__(self):
        self.version = os.environ.get("FAKE_SOURCE_VERSION", "1")
        self.day = 0

    def report(self):
        return {"version": self.version}

    def advance(self, days):
        self.day += days
        return {"day": self.day}


def step(ctx, days):
    script = session(ctx, "source", "run")
    source = None
    if ctx["plan"]["fresh"]:
        source = FakeSource()
        script.verify(source.report())
    stamp = script.take("w0", {"days": days})
    answer = script.call("advance", {"days": days}, lambda: source.advance(days))
    script.close()
    return {**answer, **stamp}
'''

JOINT_ADAPTER = '''
from bricks.game import run

from ..tape import follow


def step(ctx, days):
    flags = follow(ctx, "game")
    return run({"days": days}, fresh=flags["fresh"], record=flags["record"])
'''

SCENARIO = '''
import json, sys
from bricks.flow import run

option = json.loads(sys.argv[1])
try:
    out = run({"days": 2}, **option)
    print(json.dumps({"ok": out}))
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
'''


class PortsEndToEnd(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copy(ROOT / "AGENTS.md", self.root / "AGENTS.md")
        (self.root / "bricks").mkdir()
        for name in ("AGENTS.md", "__init__.py"):
            shutil.copy(ROOT / "bricks" / name, self.root / "bricks" / name)
        template = ROOT / "bricks/example_brick"
        for name in ("game", "flow"):
            shutil.copytree(template, self.root / "bricks" / name, ignore=shutil.ignore_patterns("__pycache__"))
        game, flow = self.root / "bricks/game", self.root / "bricks/flow"
        (game / "input/adapters/example_source.py").unlink()
        shutil.rmtree(game / "input/tapes/example_source")
        (game / "input/adapters/source.py").write_text(SOURCE_ADAPTER, encoding="utf-8")
        (game / "src/logic.py").write_text(
            "from ..input.adapters import source\n\n\ndef execute(inputs, run_context):\n    return source.step(run_context, inputs['days'])\n",
            encoding="utf-8",
        )
        self.ports(game, '{"source": {"kind": "source", "epoch": 1, "label": {"version": "1"}, "followable": True}}')
        (flow / "input/adapters/example_source.py").unlink()
        (flow / "input/adapters/game.py").write_text(JOINT_ADAPTER, encoding="utf-8")
        (flow / "src/logic.py").write_text(
            "from ..input.adapters import game\n\n\ndef execute(inputs, run_context):\n    return game.step(run_context, inputs['days'])\n",
            encoding="utf-8",
        )
        self.ports(flow, '{"game": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("source",)}}')
        shutil.rmtree(flow / "input/tapes")

    @staticmethod
    def ports(brick: Path, value: str) -> None:
        path = brick / "contract.py"
        text = path.read_text(encoding="utf-8")
        node = next(n for n in ast.parse(text).body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "PORTS")
        lines = text.splitlines(keepends=True)
        lines[node.lineno - 1 : node.end_lineno] = [f"PORTS: dict[str, dict] = {value}\n"]
        path.write_text("".join(lines), encoding="utf-8")

    def go(self, option: dict, **env: str) -> dict:
        done = subprocess.run(
            [sys.executable, "-c", SCENARIO, json.dumps(option)], cwd=self.root, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(self.root), "PYTHONDONTWRITEBYTECODE": "1", "BRICK_TEST": "1", **env},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def records(self, brick: str) -> list[dict]:
        folder = self.root / "bricks" / brick / "runner/ledger"
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.local.json"))]

    def take(self) -> Path:
        return self.root / "bricks/game/input/tapes/source/run-w0.json"

    def test_the_repository_lints_clean(self) -> None:
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))

    def test_record_through_the_joint_writes_the_take_and_the_flow_names_it_in_followed(self) -> None:
        out = self.go({"record": ["game.source"]})["ok"]
        self.assertEqual((out["day"], out["recorded"]), (2, False))
        self.assertTrue(self.take().is_file())
        (entry,) = self.records("flow")
        self.assertEqual((entry["fresh"], entry["record"], entry["followed"]), ([], [], ["game.source"]))
        (entry,) = self.records("game")
        self.assertEqual((entry["fresh"], entry["record"]), (["source"], ["source"]))
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))

    def test_replay_is_the_default_and_needs_no_source(self) -> None:
        self.go({"record": ["game.source"]})
        out = self.go({}, FAKE_SOURCE_VERSION="999")["ok"]
        self.assertEqual((out["day"], out["recorded"]), (2, True))
        self.assertEqual(self.records("flow")[-1]["followed"], [])
        self.assertEqual(self.records("game")[-1]["fresh"], [])

    def test_true_never_reaches_a_sibling(self) -> None:
        self.go({"record": ["game.source"]})
        before = self.take().read_text(encoding="utf-8")
        out = self.go({"fresh": True})["ok"]
        self.assertEqual(out["recorded"], True)
        self.assertEqual(self.records("game")[-1]["fresh"], [])
        self.assertEqual(self.take().read_text(encoding="utf-8"), before)

    def test_fresh_passes_down_by_name_without_changing_the_takes(self) -> None:
        self.go({"record": ["game.source"]})
        before = self.take().read_text(encoding="utf-8")
        out = self.go({"fresh": ["game.source"]})["ok"]
        self.assertEqual(out["recorded"], False)
        self.assertEqual(self.records("flow")[-1]["followed"], ["game.source"])
        self.assertEqual(self.records("game")[-1]["fresh"], ["source"])
        self.assertEqual(self.take().read_text(encoding="utf-8"), before)

    def test_star_reaches_every_followed_source(self) -> None:
        out = self.go({"record": ["*"]})["ok"]
        self.assertEqual(out["recorded"], False)
        self.assertTrue(self.take().is_file())

    def test_a_missing_take_is_an_error_not_a_fresh_call(self) -> None:
        result = self.go({})
        self.assertEqual(result["error"], "TapeError")
        self.assertIn("take does not exist", result["message"])

    def test_a_name_the_flow_does_not_follow_is_an_error(self) -> None:
        result = self.go({"fresh": ["game.nothing"]})
        self.assertEqual(result["error"], "ValueError")
        self.assertIn("fresh= names 'game.nothing'", result["message"])

    def test_a_changed_label_makes_the_take_stale_with_a_clear_error(self) -> None:
        self.go({"record": ["game.source"]})
        self.ports(self.root / "bricks/game", '{"source": {"kind": "source", "epoch": 1, "label": {"version": "2"}, "followable": True}}')
        result = self.go({})
        self.assertEqual(result["error"], "TapeError")
        self.assertIn('label.version: recorded "1", requested "2"', result["message"])
        self.assertTrue(any("take label differs from port 'source'" in e for e in lint_bricks.lint_repo(self.root).errors))

    def test_a_bumped_epoch_makes_the_take_stale_and_the_lint_says_so_first(self) -> None:
        self.go({"record": ["game.source"]})
        self.ports(self.root / "bricks/game", '{"source": {"kind": "source", "epoch": 2, "label": {"version": "1"}, "followable": True}}')
        result = self.go({})
        self.assertIn("take recorded at epoch 1, port 'source' is at epoch 2", result["message"])
        self.assertTrue(any("take recorded at epoch 1, port 'source' is at epoch 2" in e for e in lint_bricks.lint_repo(self.root).errors))

    def test_fresh_refuses_a_source_that_does_not_match_the_label(self) -> None:
        self.go({"record": ["game.source"]})
        result = self.go({"fresh": ["game.source"]}, FAKE_SOURCE_VERSION="2")
        self.assertEqual(result["error"], "LabelMismatch")
        self.assertIn('version: recorded against "1", the source reports "2"', result["message"])

    def test_record_refuses_a_mismatching_source_too_and_writes_nothing(self) -> None:
        result = self.go({"record": ["game.source"]}, FAKE_SOURCE_VERSION="2")
        self.assertEqual(result["error"], "LabelMismatch")
        self.assertFalse((self.root / "bricks/game/input/tapes/source").exists())

    def test_a_pin_that_falls_behind_the_interface_version_is_a_lint_error(self) -> None:
        game = self.root / "bricks/game/contract.py"
        game.write_text(game.read_text(encoding="utf-8").replace("CONTRACT_VERSION = 1", "CONTRACT_VERSION = 2"), encoding="utf-8")
        self.assertTrue(any("joint 'game' pins version 1, but 'game' is at version 2" in e for e in lint_bricks.lint_repo(self.root).errors))


PLANNER_LOGIC = """
from ..contract import BrickInput, BrickOutput


def execute(inputs, run_context):
    return {"steps": [day * 2 for day in range(inputs["days"])]}
"""

PLAN_ADAPTER = """
from bricks.planner import run


def plan(ctx, days):
    return run({"days": days})
"""

SEALED_SCENARIO = """
import json
from bricks.flow import run
from bricks.planner import run as plan

out = {"flow": run({"days": 3}, fresh=True), "planner": plan({"days": 2})}
try:
    plan({"days": 1}, fresh={"anything"})
except ValueError as exc:
    out["error"] = str(exc)
print(json.dumps(out))
"""


class SealedEndToEnd(unittest.TestCase):
    """A sealed brick has no ports, no adapters and no tapes, and an open brick reaches it through a joint."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copy(ROOT / "AGENTS.md", self.root / "AGENTS.md")
        (self.root / "bricks").mkdir()
        for name in ("AGENTS.md", "__init__.py"):
            shutil.copy(ROOT / "bricks" / name, self.root / "bricks" / name)
        template = ROOT / "bricks/example_brick"
        for name in ("planner", "flow"):
            shutil.copytree(template, self.root / "bricks" / name, ignore=shutil.ignore_patterns("__pycache__"))
        planner, flow = self.root / "bricks/planner", self.root / "bricks/flow"
        for relative in ("input/adapters", "input/tapes"):
            shutil.rmtree(planner / relative)
        (planner / "input/tape.py").unlink()
        PortsEndToEnd.ports(planner, "{}")
        (planner / "src/logic.py").write_text(PLANNER_LOGIC, encoding="utf-8")
        (flow / "input/adapters/example_source.py").unlink()
        shutil.rmtree(flow / "input/tapes")
        (flow / "input/adapters/planner.py").write_text(PLAN_ADAPTER, encoding="utf-8")
        PortsEndToEnd.ports(flow, '{"planner": {"kind": "joint", "policy": "eventual", "pin": 1}}')
        (flow / "src/logic.py").write_text(
            "from ..input.adapters import planner\n\n\ndef execute(inputs, run_context):\n    return planner.plan(run_context, inputs['days'])\n",
            encoding="utf-8",
        )

    def go(self) -> dict:
        done = subprocess.run(
            [sys.executable, "-c", SEALED_SCENARIO], cwd=self.root, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_the_repository_lints_clean_and_the_shapes_are_what_they_should_be(self) -> None:
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))
        from tools import brick_contract

        self.assertEqual(brick_contract.brick_shape(self.root / "bricks/planner"), "sealed")
        self.assertEqual(brick_contract.brick_shape(self.root / "bricks/flow"), "open")

    def test_the_flow_reaches_the_sealed_brick_and_a_sealed_brick_never_tracks_a_clean_run(self) -> None:
        out = self.go()
        self.assertEqual(out["flow"], {"steps": [0, 2, 4]})
        self.assertEqual(out["planner"], {"steps": [0, 2]})
        ledger = self.root / "bricks/planner/runner/ledger"
        self.assertEqual(sorted(path.name.endswith(".local.json") for path in ledger.glob("*.json")), [True, True])
        self.assertEqual(out["error"], "fresh= names 'anything', which is not a source port of this brick or one it follows (known: none)")

    def test_a_sealed_brick_that_gains_a_clock_or_a_port_is_caught(self) -> None:
        logic = self.root / "bricks/planner/src/logic.py"
        logic.write_text(logic.read_text(encoding="utf-8") + "\nimport time\n", encoding="utf-8")
        self.assertTrue(any("sealed brick imports 'time' outside runner/" in e for e in lint_bricks.lint_repo(self.root).errors))


if __name__ == "__main__":
    unittest.main()
