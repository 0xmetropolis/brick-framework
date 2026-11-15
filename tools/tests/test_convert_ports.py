"""Tests for tools/convert_ports.py, the one-time conversion to the ports format."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from bricks.example_brick.input import tape
from tools import brick_contract, convert_ports, lint_bricks

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "bricks/example_brick"

OLD_CONTRACT = '''"""Typed, versioned boundary and ownership declarations for this brick."""

from typing import TypedDict


CONTRACT_VERSION = {version}

# The enforcement class this brick declares for itself.
LANE = "{lane}"

DETERMINISTIC = True

# True when this brick is a Live Adapter.
LIVE_ADAPTER = {live}

# A workflow only: the Live Adapter siblings it passes its mode to.
MODE_PASSING: tuple[str, ...] = {passing}

# Map sibling brick names to "eventual" or "orchestrated".
SIBLING_DEPENDENCIES: dict[str, str] = {deps}

# Stable identifiers for application state owned exclusively by this brick.
# Brick-local evidence under input/data and runner/runs is owned implicitly.
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    """Input accepted by run()."""


class BrickOutput(TypedDict):
    """Output returned by run()."""
'''

OLD_CONFIG = """brick: {name}

retention:
  runs: 10
  saved_examples_per_adapter: 3

limits:
  max_evidence_bytes: 122880  # 120 KiB
  max_run_record_bytes: 122880  # 120 KiB
"""

OLD_SOURCE = '''from ..evidence import through


def fetch(ctx, request):
    return through(ctx, "{adapter}", "default", request, dict{extra})
'''

OLD_JOINT = '''from bricks.{sibling} import run
from ..evidence import pass_mode


def step(ctx, request):
    flags = pass_mode(ctx, "{sibling}")
    return run(request, fresh=flags["fresh"], save=flags["save"])
'''

NEW_SOURCE = '''from ..tape import once


def fetch(ctx, request):
    return once(ctx, "{adapter}", "default", request, dict)
'''

NEW_JOINT = '''from bricks.{sibling} import run
from ..tape import follow


def step(ctx, request):
    flags = follow(ctx, "{sibling}")
    return run(request, fresh=flags["fresh"], record=flags["record"])
'''


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def old_take(adapter: str, case: str, request: dict, run_id: str = "20260101-000000-000000-ab12", **extra) -> str:
    record = {
        "adapter": adapter, "capture_run_id": run_id, "case": case, "contract_version": 1,
        "request": request, "response": {"ok": 1}, "schema_version": 1, **extra,
    }
    return json.dumps(record, indent=2, sort_keys=True) + "\n"


class ConvertCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copy(ROOT / "AGENTS.md", self.root / "AGENTS.md")
        (self.root / "bricks").mkdir()
        shutil.copy(ROOT / "bricks/AGENTS.md", self.root / "bricks/AGENTS.md")
        shutil.copy(ROOT / "bricks/__init__.py", self.root / "bricks/__init__.py")

    def old_brick(
        self, name: str, *, lane: str = "strict", live: bool = False, passing: str = "()", deps: str = "{}",
        version: int = 1, sources: tuple = ("feed",), joints: tuple = (),
    ) -> Path:
        """A brick in the previous format, built from the files that format had."""
        brick = self.root / "bricks" / name
        for relative in ("__init__.py", "runner/__init__.py", "src/logic.py", "input/AGENTS.md", "runner/AGENTS.md", "src/AGENTS.md"):
            (brick / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TEMPLATE / relative, brick / relative)
        write(brick / "contract.py", OLD_CONTRACT.format(version=version, lane=lane, live=live, passing=passing, deps=deps))
        write(brick / "input/config.yml", OLD_CONFIG.format(name=name))
        write(brick / "input/evidence.py", "# BOILERPLATE: evidence@5\nx = 1\n")
        write(brick / "runner/run.py", "# BOILERPLATE: runner.run@4\nx = 1\n")
        write(brick / "runner/rng.py", "# BOILERPLATE: runner.rng@1\nx = 1\n")
        write(brick / "input/data/.gitkeep", "")
        write(brick / "runner/runs/.gitignore", "*.test.json\n")
        write(brick / "runner/runs/.gitkeep", "")
        for source in sources:
            write(brick / f"input/adapters/{source}.py", OLD_SOURCE.format(adapter=source, extra=""))
        for sibling in joints:
            write(brick / f"input/adapters/{sibling}.py", OLD_JOINT.format(sibling=sibling))
        return brick

    def convert(self) -> convert_ports.Report:
        return convert_ports.convert(self.root)

    def ports(self, name: str) -> dict:
        return brick_contract.brick_ports(self.root / "bricks" / name)

    def takes(self, name: str, source: str) -> dict[str, dict]:
        folder = self.root / "bricks" / name / "input/tapes" / source
        return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(folder.glob("*.json"))}


class ManifestTests(ConvertCase):
    def test_a_source_adapter_becomes_a_source_port_with_epoch_one(self) -> None:
        self.old_brick("pricing")
        self.convert()
        self.assertEqual(self.ports("pricing"), {"feed": {"kind": "source", "epoch": 1}})

    def test_the_old_declarations_and_their_comments_are_gone_and_the_ports_are_documented(self) -> None:
        brick = self.old_brick("pricing")
        self.convert()
        text = (brick / "contract.py").read_text(encoding="utf-8")
        for old in ("SIBLING_DEPENDENCIES", "LIVE_ADAPTER", "MODE_PASSING", "LANE", "Live Adapter", "enforcement class"):
            self.assertNotIn(old, text)
        self.assertIn("DETERMINISTIC = True", text)
        self.assertIn(convert_ports.PORTS_COMMENT, text)
        self.assertIn("Brick-local takes under input/tapes and entries under runner/ledger", text)
        self.assertIn(convert_ports.VERSION_COMMENT + "CONTRACT_VERSION = 1", text)

    def test_the_comments_it_writes_are_the_templates_own(self) -> None:
        template = (TEMPLATE / "contract.py").read_text(encoding="utf-8")
        self.assertIn(convert_ports.PORTS_COMMENT, template)
        self.assertIn(convert_ports.VERSION_COMMENT, template)
        self.assertIn(convert_ports.OWNED_STATE_NEW, template)

    def test_a_live_adapter_makes_its_sources_followable(self) -> None:
        self.old_brick("game", live=True, sources=("source",))
        self.convert()
        self.assertEqual(self.ports("game"), {"source": {"kind": "source", "epoch": 1, "followable": True}})

    def test_a_sibling_adapter_becomes_a_joint_pinned_at_the_siblings_version(self) -> None:
        self.old_brick("pricing", version=3)
        self.old_brick("checkout", sources=(), joints=("pricing",), deps='{"pricing": "eventual"}')
        self.convert()
        self.assertEqual(self.ports("checkout"), {"pricing": {"kind": "joint", "policy": "eventual", "pin": 3}})

    def test_mode_passing_becomes_follow_on_the_siblings_sources(self) -> None:
        self.old_brick("game", live=True, sources=("source", "audio"))
        self.old_brick("flow", lane="workflow", sources=(), joints=("game",), deps='{"game": "orchestrated"}', passing='("game",)')
        self.convert()
        self.assertEqual(
            self.ports("flow"), {"game": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("audio", "source")}}
        )

    def test_a_joint_adapter_named_otherwise_is_renamed_for_its_sibling(self) -> None:
        self.old_brick("pricing")
        brick = self.old_brick("checkout", sources=(), deps='{"pricing": "eventual"}')
        write(brick / "input/adapters/price.py", OLD_JOINT.format(sibling="pricing"))
        report = self.convert()
        self.assertTrue((brick / "input/adapters/pricing.py").is_file())
        self.assertFalse((brick / "input/adapters/price.py").exists())
        self.assertTrue(any("renamed adapter price.py to pricing.py" in item for item in report.done))

    def test_an_undeclared_sibling_import_is_declared_eventual_and_noted(self) -> None:
        self.old_brick("pricing")
        self.old_brick("checkout", sources=(), joints=("pricing",))
        report = self.convert()
        self.assertEqual(self.ports("checkout")["pricing"]["policy"], "eventual")
        self.assertTrue(any("did not declare it" in note for note in report.notes))

    def test_a_pure_brick_has_no_ports_and_no_tape_loader(self) -> None:
        brick = self.old_brick("calc", lane="pure", sources=())
        self.convert()
        self.assertEqual(self.ports("calc"), {})
        self.assertIn("PORTS: dict[str, dict] = {}", (brick / "contract.py").read_text(encoding="utf-8"))
        self.assertFalse((brick / "input/tape.py").exists())
        self.assertFalse((brick / "input/evidence.py").exists())

    def test_a_workflow_without_a_main_is_told_it_is_open_now(self) -> None:
        self.old_brick("flow", lane="workflow")
        report = self.convert()
        self.assertTrue(any("was a workflow but has no __main__.py" in note for note in report.notes))

    def test_a_strict_brick_with_nothing_to_reach_is_told_it_is_sealed_now(self) -> None:
        self.old_brick("plain", sources=())
        report = self.convert()
        self.assertTrue(any("is a sealed brick now" in note for note in report.notes))


class TakeTests(ConvertCase):
    def test_takes_move_to_schema_two_under_tapes(self) -> None:
        brick = self.old_brick("pricing")
        write(brick / "input/data/feed/day1.json", old_take("feed", "day1", {"day": 1}))
        write(brick / "input/data/feed/day2.json", old_take("feed", "day2", {"day": 2}, run_id="run-9"))
        self.convert()
        takes = self.takes("pricing", "feed")
        self.assertEqual(sorted(takes), ["day1", "day2"])
        self.assertEqual(
            takes["day2"],
            {
                "epoch": 1, "recorded_in": "run-9", "request": {"day": 2}, "response": {"ok": 1},
                "schema_version": 2, "source": "feed", "take": "day2",
            },
        )
        self.assertFalse((brick / "input/data").exists())
        self.assertTrue((brick / "input/tapes/.gitkeep").is_file())
        text = (brick / "input/tapes/feed/day1.json").read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")

    def test_an_error_take_keeps_its_error(self) -> None:
        brick = self.old_brick("pricing")
        record = json.loads(old_take("feed", "bad", {}))
        del record["response"]
        record["error"] = {"type": "ValueError"}
        write(brick / "input/data/feed/bad.json", json.dumps(record, indent=2, sort_keys=True) + "\n")
        self.convert()
        self.assertEqual(self.takes("pricing", "feed")["bad"]["error"], {"type": "ValueError"})

    def test_a_unanimous_fingerprint_becomes_the_ports_label_and_the_takes_label(self) -> None:
        brick = self.old_brick("game", sources=("source",))
        for case in ("w0", "w1"):
            write(brick / f"input/data/source/{case}.json", old_take("source", case, {"fingerprint": {"version": "0.71"}, "window": case}))
        self.convert()
        self.assertEqual(self.ports("game")["source"]["label"], {"version": "0.71"})
        self.assertEqual(self.takes("game", "source")["w0"]["request"], {"label": {"version": "0.71"}, "window": "w0"})

    def test_takes_without_a_fingerprint_stay_unlabelled(self) -> None:
        brick = self.old_brick("game", sources=("source",))
        write(brick / "input/data/source/w0.json", old_take("source", "w0", {"window": "w0"}))
        self.convert()
        self.assertNotIn("label", self.ports("game")["source"])
        self.assertEqual(self.takes("game", "source")["w0"]["request"], {"window": "w0"})

    def test_different_fingerprints_set_no_label_and_say_so(self) -> None:
        brick = self.old_brick("game", sources=("source",))
        write(brick / "input/data/source/a.json", old_take("source", "a", {"fingerprint": {"v": 1}}))
        write(brick / "input/data/source/b.json", old_take("source", "b", {"fingerprint": {"v": 2}}))
        report = self.convert()
        self.assertNotIn("label", self.ports("game")["source"])
        self.assertTrue(any("carry different fingerprints" in note for note in report.notes))

    def test_a_fingerprint_in_the_adapter_names_the_label_when_there_are_no_takes(self) -> None:
        brick = self.old_brick("game", sources=())
        write(brick / "input/adapters/source.py", OLD_SOURCE.format(adapter="source", extra=", fingerprint={'api': 'v2'}"))
        self.convert()
        self.assertEqual(self.ports("game")["source"]["label"], {"api": "v2"})

    def test_a_tape_named_otherwise_in_the_adapter_moves_to_the_adapters_own_folder(self) -> None:
        brick = self.old_brick("game", sources=())
        write(brick / "input/adapters/source.py", OLD_SOURCE.format(adapter="game_source", extra=""))
        write(brick / "input/data/game_source/w0.json", old_take("game_source", "w0", {}))
        report = self.convert()
        self.assertEqual(sorted(self.takes("game", "source")), ["w0"])
        self.assertEqual(self.takes("game", "source")["w0"]["source"], "source")
        self.assertTrue(any("names its tape 'game_source'" in note for note in report.notes))

    def test_a_folder_no_adapter_claims_is_kept_and_noted(self) -> None:
        brick = self.old_brick("pricing")
        write(brick / "input/data/orphan/x.json", old_take("orphan", "x", {}))
        report = self.convert()
        self.assertEqual(sorted(self.takes("pricing", "orphan")), ["x"])
        self.assertTrue(any("no adapter file claims the old data folder 'orphan'" in note for note in report.notes))

    def test_a_take_that_is_not_schema_one_is_left_alone_and_noted(self) -> None:
        brick = self.old_brick("pricing")
        write(brick / "input/data/feed/odd.json", '{"schema_version": 7}\n')
        report = self.convert()
        self.assertTrue((brick / "input/data/feed/odd.json").is_file())
        self.assertTrue(any("is not a schema 1 take" in note for note in report.notes))


class LedgerAndConfigTests(ConvertCase):
    def test_old_records_become_tracked_or_local_entries_by_the_new_policy(self) -> None:
        brick = self.old_brick("pricing")
        runs = brick / "runner/runs"
        write(runs / "r1.json", json.dumps({"run_id": "r1", "kind": "run", "mode": "saved", "input": {}, "output": {}}))
        write(runs / "r2.json", json.dumps({"run_id": "r2", "kind": "run", "mode": "save", "input": {}, "output": {}, "fresh_siblings": ["x"]}))
        write(runs / "r3.json", json.dumps({"run_id": "r3", "kind": "run", "mode": "saved", "input": {}, "error": {"type": "X", "message": "m"}}))
        write(runs / "r4.test.json", json.dumps({"run_id": "r4", "kind": "test", "mode": "fresh", "input": {}, "output": {}}))
        report = self.convert()
        ledger = brick / "runner/ledger"
        self.assertEqual(sorted(path.name for path in ledger.glob("*.json")), ["r1.local.json", "r2.json", "r3.json", "r4.local.json"])
        entry = json.loads((ledger / "r2.json").read_text(encoding="utf-8"))
        self.assertEqual((entry["fresh"], entry["record"], entry["followed"]), (["feed"], ["feed"], []))
        self.assertNotIn("kind", entry)
        self.assertNotIn("mode", entry)
        self.assertNotIn("fresh_siblings", entry)
        self.assertEqual((ledger / ".gitignore").read_text(encoding="utf-8"), "*.local.json\n")
        self.assertFalse((brick / "runner/runs").exists())
        self.assertTrue((ledger / ".gitkeep").is_file())
        self.assertTrue(any("2 tracked entries, 2 local" in item for item in report.done))

    def test_config_keys_are_renamed_in_place(self) -> None:
        brick = self.old_brick("pricing")
        self.convert()
        text = (brick / "input/config.yml").read_text(encoding="utf-8")
        self.assertEqual(
            text,
            "brick: pricing\n\nretention:\n  entries: 10\n  takes_per_tape: 3\n\nlimits:\n"
            "  max_take_bytes: 122880  # 120 KiB\n  max_entry_bytes: 122880  # 120 KiB\n",
        )

    def test_the_boilerplate_becomes_the_frameworks_copies(self) -> None:
        brick = self.old_brick("pricing")
        self.convert()
        for relative in convert_ports.BOILERPLATE:
            self.assertEqual((brick / relative).read_bytes(), (TEMPLATE / relative).read_bytes(), relative)
        self.assertFalse((brick / "input/evidence.py").exists())


class WholeProjectTests(ConvertCase):
    def project(self) -> None:
        game = self.old_brick("game", live=True, sources=("source",))
        write(game / "input/data/source/w0.json", old_take("source", "w0", {"fingerprint": {"version": "1"}, "window": "w0"}))
        flow = self.old_brick("flow", lane="workflow", sources=(), joints=("game",), deps='{"game": "orchestrated"}', passing='("game",)')
        write(flow / "__main__.py", "from . import run\n\nrun({})\n")
        write(game / "runner/runs/r1.json", json.dumps({"run_id": "r1", "kind": "run", "mode": "saved", "input": {}, "output": {}}))

    def fix_call_sites(self) -> None:
        write(self.root / "bricks/game/input/adapters/source.py", NEW_SOURCE.format(adapter="source"))
        write(self.root / "bricks/flow/input/adapters/game.py", NEW_JOINT.format(sibling="game"))

    def test_the_converted_project_lints_clean_once_the_call_sites_are_changed_by_hand(self) -> None:
        self.project()
        self.convert()
        lint = lint_bricks.lint_repo(self.root)
        self.assertTrue(all("bricks/flow/input/adapters/game.py" in item for item in lint.errors), lint.errors)
        self.assertTrue(lint.errors, "the old pass_mode call is still there and the lint says so")
        self.assertTrue(any("never plays a tape" in item for item in lint.warnings))
        self.fix_call_sites()
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))

    def test_every_converted_take_is_current_for_the_stale_tape_lint(self) -> None:
        self.project()
        self.convert()
        self.fix_call_sites()
        port = self.ports("game")["source"]
        self.assertEqual(port["label"], {"version": "1"})
        self.assertEqual(self.takes("game", "source")["w0"]["epoch"], port["epoch"])
        self.assertEqual(lint_bricks.lint_repo(self.root).errors, [])

    def test_the_manual_report_lists_each_old_name_with_its_line(self) -> None:
        self.project()
        report = self.convert()
        joined = "\n".join(report.manual)
        self.assertIn("bricks/game/input/adapters/source.py:1: from ..evidence import through", joined)
        self.assertIn("bricks/game/input/adapters/source.py:5: return through(ctx", joined)
        self.assertIn("through() is tape.once", joined)
        self.assertIn("pass_mode is tape.follow", joined)
        self.assertIn('save= is record=', joined)

    def test_converting_twice_changes_nothing_the_second_time(self) -> None:
        self.project()
        self.convert()
        self.fix_call_sites()
        before = convert_ports.tree_state(self.root / "bricks")
        report = self.convert()
        self.assertEqual(before, convert_ports.tree_state(self.root / "bricks"))
        self.assertTrue(all("already in the ports format" in item for item in report.done))

    def test_the_template_itself_is_already_converted(self) -> None:
        self.assertFalse(convert_ports.is_old(TEMPLATE))

    def test_a_dry_run_prints_what_would_change_and_touches_nothing(self) -> None:
        self.project()
        before = convert_ports.tree_state(self.root / "bricks")
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools/convert_ports.py"), "--root", str(self.root), "--dry-run"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(before, convert_ports.tree_state(self.root / "bricks"))
        self.assertIn("dry run: nothing was written", done.stdout)
        self.assertIn("added   bricks/game/input/tapes/source/w0.json", done.stdout)
        self.assertIn("removed bricks/game/input/data/source/w0.json", done.stdout)
        self.assertIn("changed bricks/game/contract.py", done.stdout)
        self.assertIn("change by hand:", done.stdout)

    def test_the_command_converts_and_ends_with_the_next_step(self) -> None:
        self.project()
        done = subprocess.run(
            [sys.executable, str(ROOT / "tools/convert_ports.py"), "--root", str(self.root)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("declared PORTS: source (source)", done.stdout)
        self.assertIn("then delete tools/convert_ports.py from the project", done.stdout)
        self.assertTrue((self.root / "bricks/game/input/tapes/source/w0.json").is_file())

    def test_a_project_with_no_bricks_folder_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            convert_ports.convert(self.root / "nowhere")


class ExampleEvidenceTests(ConvertCase):
    def test_the_committed_example_take_is_what_the_converter_makes_of_the_old_one(self) -> None:
        brick = self.old_brick("example_brick", sources=("example_source",))
        write(
            brick / "input/data/example_source/default.json",
            old_take("example_source", "default", {"purpose": "example"}, response={"value": 1}),
        )
        self.convert()
        converted = (brick / "input/tapes/example_source/default.json").read_text(encoding="utf-8")
        self.assertEqual(converted, (TEMPLATE / "input/tapes/example_source/default.json").read_text(encoding="utf-8"))
        self.assertEqual((brick / "contract.py").read_text(encoding="utf-8").count("PORTS"), (TEMPLATE / "contract.py").read_text(encoding="utf-8").count("PORTS"))

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
