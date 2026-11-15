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

    def test_brick_ports_reads_declared_ports_and_tolerates_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            brick = Path(tmp)
            self.assertEqual(brick_contract.brick_ports(brick), {})
            (brick / "contract.py").write_text(
                'PORTS = {"feed": {"kind": "source"}, "other": {"kind": "joint", "policy": "eventual", "pin": 1}, "bad": 3}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                brick_contract.brick_ports(brick),
                {"feed": {"kind": "source"}, "other": {"kind": "joint", "policy": "eventual", "pin": 1}},
            )
            (brick / "contract.py").write_text("PORTS = make()\n", encoding="utf-8")
            self.assertEqual(brick_contract.brick_ports(brick), {})

    def test_graph_draws_joints_with_their_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bricks = Path(tmp) / "bricks"
            for name, ports in (
                ("flow", '{"lib": {"kind": "joint", "policy": "orchestrated", "pin": 1}, "feed": {"kind": "source"}}'),
                ("lib", "{}"),
            ):
                (bricks / name).mkdir(parents=True)
                (bricks / name / "contract.py").write_text(f"PORTS = {ports}\n", encoding="utf-8")
            contracts = {name: graph_bricks.read_contract(bricks / name) for name in ("flow", "lib")}
        self.assertIn("flow --> lib", graph_bricks.render(contracts))


if __name__ == "__main__":
    unittest.main()
