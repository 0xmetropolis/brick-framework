"""The committed template brick: its recorded take replays and is current for the stale-tape lint."""

from __future__ import annotations

from pathlib import Path
import unittest

from bricks.example_brick.input import tape
from tools import lint_bricks

ROOT = Path(__file__).resolve().parents[2]


class ExampleBrickTests(unittest.TestCase):
    def test_the_committed_take_is_schema_two_for_the_templates_port(self) -> None:
        import json

        record = json.loads((ROOT / "bricks/example_brick/input/tapes/example_source/default.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (record["schema_version"], record["source"], record["take"], record["epoch"]),
            (2, "example_source", "default", 1),
        )
        self.assertNotIn("contract_version", record)

    def test_the_committed_take_replays_through_the_templates_adapter(self) -> None:
        from bricks.example_brick.input.adapters import example_source

        context = {"run_id": "run-1", "config": {}, "plan": {"fresh": (), "record": ()}}
        self.assertEqual(example_source.fetch({"purpose": "example"}, context), {"value": 1})
        with self.assertRaises(tape.TapeError):
            example_source.fetch({"purpose": "other"}, context)

    def test_the_committed_take_is_current_for_the_stale_tape_lint(self) -> None:
        lint = lint_bricks.lint_repo(ROOT)
        self.assertEqual((lint.errors, lint.warnings), ([], []))


if __name__ == "__main__":
    unittest.main()
