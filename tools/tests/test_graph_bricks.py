"""Tests for tools/graph_bricks.py: the deterministic, static diagram of a brick project."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools import graph_bricks, mermaid
from tools.tests import sample_project

ROOT = Path(__file__).resolve().parents[2]
TOOL = str(ROOT / "tools/graph_bricks.py")


def run_tool(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, TOOL, *args], capture_output=True, check=False)


class Project(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "bricks").mkdir()

    def brick(self, name: str, ports: str, extra: str = "", main: bool = False) -> Path:
        path = self.root / "bricks" / name
        path.mkdir()
        (path / "contract.py").write_text(f"PORTS = {ports}\n{extra}", encoding="utf-8")
        if main:
            (path / "__main__.py").write_text("", encoding="utf-8")
        return path

    def graph(self, format: str = "mermaid") -> str:
        bricks = graph_bricks.read_project(self.root)
        return (graph_bricks.render_dot if format == "dot" else graph_bricks.render)(bricks)


class DiagramTests(Project):
    def test_every_brick_carries_its_computed_shape(self) -> None:
        self.brick("chat", '{"x": {"kind": "source", "epoch": 1}}', main=True)
        self.brick("planner", "{}")
        self.brick("ordinary", '{"x": {"kind": "source", "epoch": 1}}')
        text = self.graph()
        self.assertIn('b_chat["chat<br/>entry"]', text)
        self.assertIn('b_planner["planner<br/>sealed"]', text)
        self.assertIn('b_ordinary["ordinary<br/>open"]', text)

    def test_a_source_shows_its_epoch_label_and_whether_it_is_followable(self) -> None:
        self.brick("pricing", '{"rates": {"kind": "source", "epoch": 2, "label": {"protocol": "p1", "api": "v2"}, "followable": True}}')
        self.assertIn('s_pricing__rates[("rates<br/>epoch 2<br/>label api=v2, protocol=p1<br/>followable")]', self.graph())

    def test_a_joint_shows_its_policy_and_pin_and_a_follow_is_a_thick_arrow_to_the_source(self) -> None:
        self.brick("assistant", '{"xai": {"kind": "source", "epoch": 1, "followable": True}}')
        self.brick("pricing", "{}")
        self.brick("chat", '{"assistant": {"kind": "joint", "policy": "orchestrated", "pin": 3, "follow": ("xai",)}}', main=True)
        self.brick("checkout", '{"pricing": {"kind": "joint", "policy": "eventual", "pin": 1}}')
        text = self.graph()
        self.assertIn('b_chat -->|"orchestrated, pin 3"| b_assistant', text)
        self.assertIn('b_chat ==>|"follows"| s_assistant__xai', text)
        self.assertIn('b_checkout -.->|"eventual, pin 1"| b_pricing', text)

    def test_a_follow_of_a_source_the_sibling_does_not_have_is_not_drawn(self) -> None:
        self.brick("assistant", "{}")
        self.brick("chat", '{"assistant": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("nothing",)}}')
        self.assertNotIn("==>", self.graph())

    def test_a_joint_to_a_brick_that_does_not_exist_is_a_marked_ghost_not_a_broken_node(self) -> None:
        self.brick("chat", '{"ghost": {"kind": "joint", "policy": "eventual", "pin": 1}}')
        text = self.graph()
        self.assertIn('b_ghost["ghost<br/>missing"]', text)
        self.assertIn('b_chat -.->|"eventual, pin 1"| b_ghost', text)

    def test_owned_state_and_declared_nondeterminism_stay_in_the_label(self) -> None:
        self.brick("load", "{}", 'OWNED_STATE = ("files:b", "database:a")\nDETERMINISTIC = False\n')
        self.assertIn("load<br/>sealed<br/>database:a<br/>files:b<br/>nondeterministic", self.graph())

    def test_the_top_level_class_marks_what_nothing_depends_on_only_when_some_are_depended_on(self) -> None:
        self.brick("lib", "{}")
        self.brick("app", '{"lib": {"kind": "joint", "policy": "eventual", "pin": 1}}')
        self.assertIn("class b_app topLevel", self.graph())
        alone = tempfile.TemporaryDirectory()
        self.addCleanup(alone.cleanup)
        (Path(alone.name) / "bricks/one").mkdir(parents=True)
        (Path(alone.name) / "bricks/one/contract.py").write_text("PORTS = {}\n", encoding="utf-8")
        self.assertNotIn("topLevel", graph_bricks.render(graph_bricks.read_project(Path(alone.name))))

    def test_a_malformed_manifest_draws_a_bare_open_brick(self) -> None:
        self.brick("odd", "make_ports()")
        self.assertIn('b_odd["odd<br/>open"]', self.graph())

    def test_nothing_is_executed(self) -> None:
        path = self.brick("sneaky", "{}", "raise SystemExit('executed')\nopen('/tmp/executed-by-graph', 'w')\n")
        (path / "__init__.py").write_text("raise SystemExit('imported')\n", encoding="utf-8")
        self.assertIn("sneaky<br/>sealed", self.graph())
        self.assertFalse(Path("/tmp/executed-by-graph").exists())


class EscapeTests(Project):
    def test_awkward_names_cannot_break_the_diagram(self) -> None:
        self.brick("end", "{}")
        self.brick("graph", "{}")
        self.brick("a-b.c", '{"x\\"y": {"kind": "source", "epoch": 1, "label": {"k": "v|w; <b> #1 %%"}}}')
        text = self.graph()
        self.assertIn('b_end["end<br/>sealed"]', text)
        self.assertIn('b_a_b_c["a-b.c<br/>open"]', text)
        self.assertIn("#quot;", text)
        self.assertIn("#124;", text)
        self.assertIn("#59;", text)
        self.assertIn("#lt;b#gt;", text)
        self.assertIn("#35;1", text)
        for line in text.splitlines()[2:]:
            self.assertNotIn("%%", line)

    def test_ids_that_collide_after_cleaning_stay_distinct_and_stable(self) -> None:
        self.brick("a-b", "{}")
        self.brick("a_b", "{}")
        first = self.graph()
        self.assertIn('b_a_b["a-b<br/>sealed"]', first)
        self.assertIn('b_a_b_2["a_b<br/>sealed"]', first)
        self.assertEqual(first, self.graph())

    def test_escape_never_escapes_its_own_entities_twice(self) -> None:
        self.assertEqual(mermaid.escape('a#b"c'), "a#35;b#quot;c")
        self.assertEqual(mermaid.escape("one\ntwo"), "one two")

    def test_identifier_is_prefixed_unique_and_asked_in_order(self) -> None:
        taken: set[str] = set()
        self.assertEqual(mermaid.identifier("b_", "end", taken), "b_end")
        self.assertEqual(mermaid.identifier("b_", "end", taken), "b_end_2")
        self.assertEqual(mermaid.identifier("b_", "x y", taken), "b_x_y")


class DotTests(Project):
    def test_dot_has_the_same_content(self) -> None:
        self.brick("assistant", '{"xai": {"kind": "source", "epoch": 2, "label": {"model": "m"}, "followable": True}}')
        self.brick("planner", "{}")
        self.brick("chat", '{"assistant": {"kind": "joint", "policy": "orchestrated", "pin": 2, "follow": ("xai",)}, "planner": {"kind": "joint", "policy": "eventual", "pin": 1}}', main=True)
        text = self.graph("dot")
        self.assertIn('"brick:chat" [shape=box, label="chat\\nentry"];', text)
        self.assertIn('"brick:planner" [shape=box, label="planner\\nsealed"];', text)
        self.assertIn('"source:assistant:xai" [shape=cylinder, label="xai\\nepoch 2\\nlabel model=m\\nfollowable"];', text)
        self.assertIn('"brick:chat" -> "brick:assistant" [label="orchestrated, pin 2"];', text)
        self.assertIn('"brick:chat" -> "brick:planner" [label="eventual, pin 1", style=dashed];', text)
        self.assertIn('"brick:chat" -> "source:assistant:xai" [label="follows", style=bold];', text)
        self.assertTrue(text.startswith("// Generated by tools/graph_bricks.py") and text.endswith("}\n"))

    def test_dot_escapes_quotes_and_backslashes(self) -> None:
        self.brick("odd", '{"x": {"kind": "source", "epoch": 1, "label": {"k": "a\\"b\\\\c"}}}')
        self.assertIn('label k=a\\"b\\\\c', self.graph("dot"))


class DeterminismTests(unittest.TestCase):
    def sample(self, parent: Path, name: str) -> Path:
        root = parent / name
        root.mkdir()
        return sample_project.build(root, record=False)

    def test_the_same_project_gives_identical_bytes_every_time_in_both_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.sample(Path(tmp), "project")
            for fmt in ("mermaid", "dot"):
                runs = [run_tool("--root", str(root), "--format", fmt) for _ in range(3)]
                self.assertTrue(all(done.returncode == 0 for done in runs), [done.stderr for done in runs])
                self.assertEqual(runs[0].stdout, runs[1].stdout)
                self.assertEqual(runs[1].stdout, runs[2].stdout)

    def test_a_copy_of_the_project_at_another_path_gives_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.sample(Path(tmp), "one")
            second = Path(tmp) / "two" / "nested"
            shutil.copytree(first, second)
            self.assertEqual(run_tool("--root", str(first)).stdout, run_tool("--root", str(second)).stdout)

    def test_the_order_files_were_created_in_does_not_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = self.sample(Path(tmp), "one")
            second = Path(tmp) / "two"
            second.mkdir()
            (second / "bricks").mkdir()
            for name in ("planner", "chat", "assistant"):
                shutil.copytree(first / "bricks" / name, second / "bricks" / name)
            self.assertEqual(run_tool("--root", str(first)).stdout, run_tool("--root", str(second)).stdout)

    def test_the_output_has_no_timestamp_and_no_path_of_this_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.sample(Path(tmp), "project")
            for fmt in ("mermaid", "dot"):
                text = run_tool("--root", str(root), "--format", fmt).stdout.decode("utf-8")
                self.assertNotIn(tmp, text)
                self.assertNotIn("/tmp", text)
                self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")

    def test_output_flag_writes_the_same_bytes_as_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.sample(Path(tmp), "project")
            target = Path(tmp) / "out" / "graph.mmd"
            done = run_tool("--root", str(root), "--output", str(target))
            self.assertEqual(done.returncode, 0)
            self.assertEqual(done.stdout, b"")
            self.assertEqual(target.read_bytes(), run_tool("--root", str(root)).stdout)

    def test_the_sample_project_diagram_is_exactly_this(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self.sample(Path(tmp), "project")
            text = run_tool("--root", str(root)).stdout.decode("utf-8")
        self.assertEqual(text, SAMPLE_DIAGRAM)


SAMPLE_DIAGRAM = (
    "%% Generated by tools/graph_bricks.py from contract.py files only. Box: a brick and its computed shape. "
    "Cylinder: a source port. Dashed arrow: eventual joint. Solid arrow: orchestrated joint. Thick arrow: a followed source.\n"
    "flowchart TD\n"
    '    b_assistant["assistant<br/>open"]\n'
    '    b_chat["chat<br/>entry"]\n'
    '    b_planner["planner<br/>sealed"]\n'
    '    s_assistant__xai[("xai<br/>epoch 2<br/>label model=grok-4<br/>followable")]\n'
    '    s_chat__history[("history<br/>epoch 1<br/>label store=v1")]\n'
    "\n"
    "    b_assistant --> s_assistant__xai\n"
    "    b_chat --> s_chat__history\n"
    '    b_chat -->|"orchestrated, pin 2"| b_assistant\n'
    '    b_chat ==>|"follows"| s_assistant__xai\n'
    '    b_chat -.->|"eventual, pin 1"| b_planner\n'
    "\n"
    "    classDef topLevel stroke-width:3px\n"
    "    class b_chat topLevel\n"
)


class CommandTests(Project):
    def test_the_committed_template_draws_its_one_source(self) -> None:
        done = run_tool()
        self.assertEqual(done.returncode, 0, done.stderr)
        text = done.stdout.decode("utf-8")
        self.assertIn('s_example_brick__example_source[("example_source<br/>epoch 1")]', text)
        self.assertTrue(text.startswith("%% Generated by tools/graph_bricks.py"))

    def test_an_empty_or_missing_bricks_folder_is_an_error(self) -> None:
        done = run_tool("--root", str(self.root))
        self.assertEqual(done.returncode, 1)
        self.assertIn("no bricks to graph", done.stderr.decode())
        done = run_tool("--root", str(self.root / "nowhere"))
        self.assertIn("bricks directory is missing", done.stderr.decode())


if __name__ == "__main__":
    unittest.main()
