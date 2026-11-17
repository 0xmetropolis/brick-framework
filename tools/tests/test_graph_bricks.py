"""Tests for tools/graph_bricks.py: the deterministic, static diagram of a brick project."""

from __future__ import annotations

from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools import graph_bricks, mermaid
from tools.tests import sample_project

ROOT = Path(__file__).resolve().parents[2]
TOOL = str(ROOT / "tools/graph_bricks.py")


def without_legend(text: str) -> str:
    """The diagram proper: everything before the legend block, in either format."""
    for marker in ("    subgraph legend[", "  subgraph cluster_legend {"):
        if marker in text:
            return text.split(marker, 1)[0]
    return text


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
        self.assertNotIn("==>", without_legend(self.graph()))

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
        self.assertNotIn("class b_one", graph_bricks.render(graph_bricks.read_project(Path(alone.name))))

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
        self.assertIn('"brick:chat" [shape=box, penwidth=3, label="chat\\nentry"];', text)
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


SAMPLE_DIAGRAM_BODY = (
    "%% Generated by tools/graph_bricks.py from contract.py files only. The legend at the end explains every encoding.\n"
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
)

MERMAID_LEGEND = (
    '    subgraph legend["Legend"]\n'
    '        lg_brick["brick name<br/>its computed shape<br/>state it owns, one line each<br/>nondeterministic: it declares DETERMINISTIC = False"]\n'
    '        lg_sealed["sealed<br/>shape of a brick with no ports: it reaches nothing outside itself"]\n'
    '        lg_entry["entry<br/>shape of a brick with a __main__.py: invoked as a whole, never depended on"]\n'
    '        lg_open["open<br/>shape of every other brick: it has ports and no __main__.py"]\n'
    '        lg_top["top-level brick<br/>thick border: no sibling depends on it"]\n'
    '        lg_missing["brick name<br/>missing<br/>dashed box: a joint names a brick that is not in the project"]\n'
    "        lg_source[(\"source port<br/>epoch N: bumped when its adapter's meaning changes<br/>label k=v: what the source is<br/>followable: a caller may ask for it fresh\")]\n"
    '        lg_own_from["brick"]\n'
    '        lg_own_to[("its source port<br/>plain arrow with no label: the brick owns the port")]\n'
    '        lg_orchestrated_from["brick"]\n'
    '        lg_orchestrated_to["sibling brick<br/>solid labelled arrow: an orchestrated joint, with the CONTRACT_VERSION it pins"]\n'
    '        lg_eventual_from["brick"]\n'
    '        lg_eventual_to["sibling brick<br/>dashed labelled arrow: an eventual joint, with the CONTRACT_VERSION it pins"]\n'
    '        lg_follow_from["brick"]\n'
    '        lg_follow_to[("followable source of a sibling<br/>thick arrow: the joint follows it, so fresh and record reach it")]\n'
    "        lg_own_from --> lg_own_to\n"
    '        lg_orchestrated_from -->|"orchestrated, pin N"| lg_orchestrated_to\n'
    '        lg_eventual_from -.->|"eventual, pin N"| lg_eventual_to\n'
    '        lg_follow_from ==>|"follows"| lg_follow_to\n'
    "    end\n"
    "\n"
    "    classDef topLevel stroke-width:3px\n"
    "    class lg_top topLevel\n"
)

SAMPLE_DIAGRAM = SAMPLE_DIAGRAM_BODY + MERMAID_LEGEND + "    class b_chat topLevel\n"


MERMAID_NODE = re.compile(r'^\w+(\[\(|\(\(|\[\[|\{\{|\{|\(\[|\[/|\[\\|\(|\[)"')
MERMAID_EDGE = re.compile(r'^\w+ ([-=.~<>ox]{2,})(\|".*"\|)? \w+$')
DOT_NODE = re.compile(r'^\s*"[^"]+" \[')
DOT_LABEL = re.compile(r'label="(?:[^"\\]|\\.)*"')
DOT_ATTRIBUTE = re.compile(r"\b(\w+)=([^,\]\s;\"]+)")
LABEL_WORDS = ("epoch", "label", "followable", "nondeterministic", "missing", "sealed", "entry", "open")


def split_mermaid(text: str) -> tuple[list[str], list[str]]:
    """(diagram lines, legend lines): the legend is its subgraph plus every line that names a legend node."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("    subgraph legend["))
    end = next(i for i in range(start, len(lines)) if lines[i] == "    end")
    after = lines[end + 1 :]
    legend = lines[start : end + 1] + [line for line in after if "lg_" in line or line.strip().startswith("classDef ")]
    body = lines[:start] + [line for line in after if "lg_" not in line]
    return body, legend


def mermaid_encodings(lines: list[str]) -> set[str]:
    """Every node shape, arrow style and class style a block of Mermaid lines uses."""
    found: set[str] = set()
    for line in (line.strip() for line in lines):
        if match := MERMAID_NODE.match(line):
            found.add(f"node {match.group(1)}")
        if match := MERMAID_EDGE.match(line):
            found.add(f"arrow {match.group(1)}")
        if line.startswith("classDef "):
            found.add(line)
    return found


def split_dot(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("  subgraph cluster_legend"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "  }")
    return lines[:start] + lines[end + 1 :], lines[start : end + 1]


def dot_encodings(lines: list[str]) -> set[str]:
    """Every node attribute and edge attribute (other than the label) a block of DOT lines uses."""
    found: set[str] = set()
    for line in lines:
        bare = DOT_LABEL.sub("", line)
        attributes = bare.split("[", 1)[1] if "[" in bare else ""
        if " -> " in line:
            found.add("arrow ->")
            found |= {f"edge {key}={value}" for key, value in DOT_ATTRIBUTE.findall(attributes)}
        elif DOT_NODE.match(line):
            found |= {f"node {key}={value}" for key, value in DOT_ATTRIBUTE.findall(attributes)}
    return found


def words_used(lines: list[str]) -> set[str]:
    text = "\n".join(lines).replace("\\n", "<br/>")
    return {word for word in LABEL_WORDS if f"<br/>{word}" in text or f'"{word}<br/>' in text}


class LegendTests(Project):
    """The legend explains every encoding the diagram uses and nothing else, in both formats."""

    def rich_project(self) -> Path:
        self.brick("assistant", '{"xai": {"kind": "source", "epoch": 2, "label": {"model": "m"}, "followable": True}}')
        self.brick("planner", "{}", 'OWNED_STATE = ("files:plans",)\nDETERMINISTIC = False\n')
        self.brick(
            "chat",
            '{"history": {"kind": "source", "epoch": 1}, "assistant": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("xai",)}, '
            '"planner": {"kind": "joint", "policy": "eventual", "pin": 1}, "ghost": {"kind": "joint", "policy": "eventual", "pin": 1}}',
            main=True,
        )
        self.brick("plain", '{"x": {"kind": "source", "epoch": 1}}')
        return self.root

    def test_the_diagram_uses_exactly_the_encodings_the_legend_explains_in_mermaid(self) -> None:
        self.rich_project()
        body, legend = split_mermaid(self.graph())
        used, explained = mermaid_encodings(body), mermaid_encodings(legend)
        self.assertEqual(used, explained, f"only in the diagram: {sorted(used - explained)}; only in the legend: {sorted(explained - used)}")
        self.assertEqual(
            used,
            {"node [", "node [(", "arrow -->", "arrow -.->", "arrow ==>", "classDef topLevel stroke-width:3px"},
        )

    def test_the_diagram_uses_exactly_the_encodings_the_legend_explains_in_dot(self) -> None:
        self.rich_project()
        body, legend = split_dot(self.graph("dot"))
        used, explained = dot_encodings(body), dot_encodings(legend)
        self.assertEqual(used, explained, f"only in the diagram: {sorted(used - explained)}; only in the legend: {sorted(explained - used)}")
        self.assertEqual(
            used,
            {"arrow ->", "node shape=box", "node shape=cylinder", "node style=dashed", "node penwidth=3", "edge style=dashed", "edge style=bold"},
        )

    def test_every_word_the_labels_use_is_explained_and_the_legend_adds_none(self) -> None:
        self.rich_project()
        for format, splitter in (("mermaid", split_mermaid), ("dot", split_dot)):
            with self.subTest(format=format):
                body, legend = splitter(self.graph(format))
                self.assertEqual(words_used(body), set(LABEL_WORDS))
                self.assertEqual(words_used(legend), words_used(body))
                text = "\n".join(legend)
                for fragment in ("epoch N", "label k=v", "followable:", "nondeterministic:", "DETERMINISTIC = False", "missing", "sealed", "entry", "open"):
                    self.assertIn(fragment, text)

    def test_a_new_encoding_in_the_diagram_that_the_legend_lacks_is_caught(self) -> None:
        self.rich_project()
        body, legend = split_mermaid(self.graph())
        drifted = [*body, '    b_x ==o b_y', '    b_x{{"hex"}}', "    classDef spare fill:#f96,stroke:#333"]
        self.assertEqual(
            mermaid_encodings(drifted) - mermaid_encodings(legend),
            {"arrow ==o", "node {{", "classDef spare fill:#f96,stroke:#333"},
        )
        dot_body, dot_legend = split_dot(self.graph("dot"))
        dot_drifted = [*dot_body, '  "a" [shape=hexagon, color=red, label="x"];', '  "a" -> "b" [style=dotted, color=blue];']
        self.assertEqual(
            dot_encodings(dot_drifted) - dot_encodings(dot_legend),
            {"node shape=hexagon", "node color=red", "edge style=dotted", "edge color=blue"},
        )

    def test_the_legend_comes_last_and_is_the_same_whatever_the_project(self) -> None:
        legends = []
        for build in (self.rich_project, lambda: self.brick("solo", "{}")):
            build()
            for format, splitter in (("mermaid", split_mermaid), ("dot", split_dot)):
                text = self.graph(format)
                self.assertIn("Legend", text)
                _, legend = splitter(text)
                legends.append((format, [line for line in legend if "legend:" in line or "lg_" in line or "Legend" in line]))
        self.assertEqual([l for f, l in legends if f == "mermaid"][0], [l for f, l in legends if f == "mermaid"][-1])
        self.assertEqual([l for f, l in legends if f == "dot"][0], [l for f, l in legends if f == "dot"][-1])

    def test_the_legend_is_in_every_diagram_even_a_project_with_nothing_to_explain(self) -> None:
        self.brick("solo", "{}")
        self.assertIn('subgraph legend["Legend"]', self.graph())
        self.assertIn("subgraph cluster_legend", self.graph("dot"))

    def test_the_legend_tables_only_name_encodings_the_renderers_have(self) -> None:
        kinds = {kind for _, kind, _ in graph_bricks.LEGEND_NODES}
        self.assertEqual(kinds, {"box", "cylinder", "missing", "top"})
        for _, _, kind, _ in graph_bricks.LEGEND_EDGES:
            self.assertIn(kind, graph_bricks.MERMAID_ARROWS)
            self.assertIn(kind, graph_bricks.DOT_EDGE_STYLES)
        ids = {ident for ident, _, _ in graph_bricks.LEGEND_NODES}
        for start, end, _, _ in graph_bricks.LEGEND_EDGES:
            self.assertLessEqual({start, end}, ids)
        self.assertEqual({kind for _, _, kind, _ in graph_bricks.LEGEND_EDGES}, set(graph_bricks.MERMAID_ARROWS))

    def test_the_legend_text_is_exactly_this(self) -> None:
        self.brick("solo", "{}")
        text = self.graph()
        self.assertIn(MERMAID_LEGEND, text)
        self.assertTrue(text.endswith("    class lg_top topLevel\n"))


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
