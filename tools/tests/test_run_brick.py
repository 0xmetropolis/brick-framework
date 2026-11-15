"""Tests for tools/run_brick.py: run one brick, in a chosen mode, from the command line."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOGIC = '''
from ..contract import BrickInput, BrickOutput


def execute(inputs, run_context):
    if inputs.get("boom"):
        raise ValueError("saved example does not exist")
    return {"mode": run_context["mode"], "echo": inputs, "raw": b"\\x00\\x01" if inputs.get("bytes") else None}
'''


class RunBrickTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "bricks").mkdir()
        shutil.copy(ROOT / "bricks/__init__.py", self.root / "bricks/__init__.py")
        shutil.copytree(ROOT / "bricks/example_brick", self.root / "bricks/demo", ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "bricks/demo/src/logic.py").write_text(LOGIC, encoding="utf-8")

    def run_tool(self, *args: str, stdin: str = "{}") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "tools/run_brick.py"), "--root", str(self.root), *args],
            input=stdin, capture_output=True, text=True, check=False,
        )

    def test_default_run_is_saved_and_reads_stdin(self) -> None:
        done = self.run_tool("demo", stdin='{"day": 5}')
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        self.assertEqual((out["mode"], out["echo"]), ("saved", {"day": 5}))

    def test_input_file_fresh_and_save_modes(self) -> None:
        path = self.root / "in.json"
        path.write_text('{"x": 1}', encoding="utf-8")
        self.assertEqual(json.loads(self.run_tool("demo", "--input", str(path), "--fresh").stdout)["mode"], "fresh")
        saved = json.loads(self.run_tool("demo", "--input", str(path), "--save").stdout)
        self.assertEqual((saved["mode"], saved["echo"]), ("save", {"x": 1}))

    def test_test_flag_writes_a_local_test_record(self) -> None:
        self.run_tool("demo", "--test")
        self.assertEqual(len(list((self.root / "bricks/demo/runner/runs").glob("*.test.json"))), 1)
        self.assertEqual(list((self.root / "bricks/demo/runner/runs").glob("[0-9]*[0-9a-f].json")), [])

    def test_bytes_in_the_output_are_kept_as_base64(self) -> None:
        out = json.loads(self.run_tool("demo", stdin='{"bytes": true}').stdout)
        self.assertEqual(out["raw"], {"base64": "AAE="})

    def test_a_failure_is_one_line_not_a_traceback(self) -> None:
        done = self.run_tool("demo", stdin='{"boom": true}')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stderr.strip(), "error: ValueError: saved example does not exist")
        self.assertNotIn("Traceback", done.stderr)

    def test_traceback_flag_shows_the_stack(self) -> None:
        done = self.run_tool("demo", "--traceback", stdin='{"boom": true}')
        self.assertIn("Traceback", done.stderr)

    def test_unknown_brick_and_bad_names_are_rejected(self) -> None:
        for name in ("nothing", "../etc", "Demo"):
            done = self.run_tool(name)
            self.assertEqual(done.returncode, 2, name)
            self.assertIn("no brick named", done.stderr)

    def test_bad_json_input_is_a_usage_error(self) -> None:
        done = self.run_tool("demo", stdin="{nope")
        self.assertEqual(done.returncode, 2)
        self.assertIn("cannot read the JSON input", done.stderr)


if __name__ == "__main__":
    unittest.main()
