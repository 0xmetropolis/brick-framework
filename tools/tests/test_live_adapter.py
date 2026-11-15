"""The Live Adapter pattern end to end, on a fake live source, in a throwaway repository.

``game`` is a Live Adapter brick: it drives a source over a session, records it through
``Transcript`` and replays by default. ``flow`` is a workflow that passes its mode to it.
The fake source stands in for what a launcher would have started.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from tools import lint_bricks

ROOT = Path(__file__).resolve().parents[2]

HOST_ADAPTER = '''
import os

from ..evidence import Transcript, provenance


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
    recorded_against = os.environ.get("FINGERPRINT_VERSION", "1")
    script = Transcript(ctx, "source", "run", fingerprint={"version": recorded_against})
    source = None
    if ctx["mode"] != "saved":
        source = FakeSource()
        script.check_source(source.report())
    prov = script.begin("w0", {"days": days})
    answer = script.op("advance", {"days": days}, lambda: source.advance(days))
    script.end()
    return {**answer, **prov}
'''

WORKFLOW_ADAPTER = '''
from bricks.game import run

from ..evidence import pass_mode


def step(ctx, days):
    flags = pass_mode(ctx, "game")
    return run({"days": days}, fresh=flags["fresh"], save=flags["save"])
'''

SCENARIO = '''
import json, os, sys
from bricks.flow import run

mode = sys.argv[1]
try:
    out = run({"days": 2}, fresh=mode in ("fresh", "save"), save=mode == "save")
    print(json.dumps({"ok": out}))
except Exception as exc:
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
'''


class LiveAdapterEndToEnd(unittest.TestCase):
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
        (game / "input/adapters/source.py").write_text(HOST_ADAPTER, encoding="utf-8")
        (game / "src/logic.py").write_text(
            "from ..input.adapters import source\n\n\ndef execute(inputs, run_context):\n    return source.step(run_context, inputs['days'])\n",
            encoding="utf-8",
        )
        self.edit(game / "contract.py", "LIVE_ADAPTER = False", "LIVE_ADAPTER = True")
        (flow / "input/adapters/example_source.py").unlink()
        (flow / "input/adapters/game.py").write_text(WORKFLOW_ADAPTER, encoding="utf-8")
        (flow / "src/logic.py").write_text(
            "from ..input.adapters import game\n\n\ndef execute(inputs, run_context):\n    return game.step(run_context, inputs['days'])\n",
            encoding="utf-8",
        )
        self.edit(flow / "contract.py", 'LANE = "strict"', 'LANE = "workflow"')
        self.edit(flow / "contract.py", "SIBLING_DEPENDENCIES: dict[str, str] = {}", 'SIBLING_DEPENDENCIES: dict[str, str] = {"game": "orchestrated"}')
        self.edit(flow / "contract.py", "MODE_PASSING: tuple[str, ...] = ()", 'MODE_PASSING: tuple[str, ...] = ("game",)')

    @staticmethod
    def edit(path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        assert old in text, (path, old)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def go(self, mode: str, **env: str) -> dict:
        done = subprocess.run(
            [sys.executable, "-c", SCENARIO, mode], cwd=self.root, capture_output=True, text=True, check=False,
            env={**os.environ, "PYTHONPATH": str(self.root), "BRICK_RUN_KIND": "test", **env},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def records(self, brick: str) -> list[dict]:
        folder = self.root / "bricks" / brick / "runner/runs"
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.test.json"))]

    def test_the_repository_lints_clean(self) -> None:
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))

    def test_save_records_through_the_sibling_and_names_it_in_the_workflow_record(self) -> None:
        out = self.go("save")["ok"]
        self.assertEqual((out["day"], out["recorded"]), (2, False))
        self.assertTrue((self.root / "bricks/game/input/data/source/run-w0.json").is_file())
        (record,) = self.records("flow")
        self.assertEqual(record["mode"], "save")
        self.assertEqual(record["fresh_siblings"], ["game"])
        self.assertEqual(self.records("game")[0]["mode"], "save")

    def test_replay_is_the_default_and_needs_no_source(self) -> None:
        self.go("save")
        out = self.go("saved", FAKE_SOURCE_VERSION="999")["ok"]
        self.assertEqual((out["day"], out["recorded"]), (2, True))
        flow_records = self.records("flow")
        self.assertNotIn("fresh_siblings", flow_records[-1])
        self.assertEqual(self.records("game")[-1]["mode"], "saved")

    def test_fresh_passes_down_without_changing_the_examples(self) -> None:
        self.go("save")
        before = (self.root / "bricks/game/input/data/source/run-w0.json").read_text(encoding="utf-8")
        out = self.go("fresh")["ok"]
        self.assertEqual(out["recorded"], False)
        self.assertEqual(self.records("flow")[-1]["fresh_siblings"], ["game"])
        self.assertEqual(self.records("game")[-1]["mode"], "fresh")
        self.assertEqual((self.root / "bricks/game/input/data/source/run-w0.json").read_text(encoding="utf-8"), before)

    def test_a_missing_example_is_an_error_not_a_live_call(self) -> None:
        result = self.go("saved")
        self.assertEqual(result["error"], "EvidenceError")
        self.assertIn("saved example does not exist", result["message"])

    def test_a_changed_fingerprint_makes_the_recording_stale_with_a_clear_error(self) -> None:
        self.go("save")
        result = self.go("saved", FINGERPRINT_VERSION="2")
        self.assertEqual(result["error"], "EvidenceError")
        self.assertIn('fingerprint.version: saved "1", requested "2"', result["message"])

    def test_fresh_refuses_a_source_that_does_not_match_the_fingerprint(self) -> None:
        self.go("save")
        result = self.go("fresh", FAKE_SOURCE_VERSION="2")
        self.assertEqual(result["error"], "SourceMismatch")
        self.assertIn('version: recorded against "1", the source reports "2"', result["message"])

    def test_save_refuses_a_mismatching_source_too_and_writes_nothing(self) -> None:
        result = self.go("save", FAKE_SOURCE_VERSION="2")
        self.assertEqual(result["error"], "SourceMismatch")
        self.assertFalse((self.root / "bricks/game/input/data/source").exists())


if __name__ == "__main__":
    unittest.main()
