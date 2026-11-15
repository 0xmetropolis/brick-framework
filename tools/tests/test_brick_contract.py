"""Tests for tools/brick_contract.py, the one contract reader the tools share."""

from __future__ import annotations

import ast
from pathlib import Path
import tempfile
import unittest

from tools import brick_contract, graph_bricks, lint_bricks


class ContractReaderTests(unittest.TestCase):
    def test_reads_plain_and_annotated_literals(self) -> None:
        tree = ast.parse('LANE = "pure"\nOWNED_STATE: tuple[str, ...] = ("files:x",)\n')
        self.assertEqual(brick_contract.assigned_literal(tree, "LANE"), "pure")
        self.assertEqual(brick_contract.assigned_literal(tree, "OWNED_STATE"), ("files:x",))
        self.assertIsNone(brick_contract.assigned_literal(tree, "MISSING"))

    def test_non_literal_is_none(self) -> None:
        self.assertIsNone(brick_contract.assigned_literal(ast.parse("LANE = compute()\n"), "LANE"))

    def test_contract_value_reads_a_file_and_tolerates_a_missing_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brick = Path(tmp)
            self.assertIsNone(brick_contract.contract_value(brick, "LANE"))
            (brick / "contract.py").write_text('LANE = "workflow"\n', encoding="utf-8")
            self.assertEqual(brick_contract.contract_value(brick, "LANE"), "workflow")
            (brick / "contract.py").write_text("LANE = (\n", encoding="utf-8")
            self.assertIsNone(brick_contract.contract_value(brick, "LANE"))

    def test_lint_and_graph_use_the_shared_reader(self) -> None:
        self.assertIs(lint_bricks.assigned_literal, brick_contract.assigned_literal)
        self.assertIs(graph_bricks.assigned_literal, brick_contract.assigned_literal)

    def test_graph_marks_declared_nondeterminism(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brick = Path(tmp) / "bricks" / "load"
            brick.mkdir(parents=True)
            (brick / "contract.py").write_text("DETERMINISTIC = False\n", encoding="utf-8")
            contracts = {"load": graph_bricks.read_contract(brick)}
        self.assertIn("nondeterministic", graph_bricks.render(contracts))


if __name__ == "__main__":
    unittest.main()
