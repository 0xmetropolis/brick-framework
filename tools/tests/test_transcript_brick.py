"""Tests for tools/transcript_brick.py: one sample followed through a multi-brick project, hop by hop."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tools import transcript_brick
from tools.tests import sample_project

ROOT = Path(__file__).resolve().parents[2]
TOOL = str(ROOT / "tools/transcript_brick.py")
SAMPLE = {"user": "ada", "goal": "ship"}


def run_tool(root: Path, *args: str, sample: object = SAMPLE) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, TOOL, "chat", "--root", str(root), *args],
        input=json.dumps(sample), capture_output=True, text=True, check=False,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"},
    )


class Project(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        (self.tmp / "project").mkdir()
        self.root = sample_project.build(self.tmp / "project")

    def report(self, name: str = "stdin") -> tuple[str, dict]:
        folder = self.root / "reports/transcripts"
        return (folder / f"chat-{name}.md").read_text(encoding="utf-8"), json.loads((folder / f"chat-{name}.json").read_text(encoding="utf-8"))

    def takes(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in sorted(self.root.rglob("tapes/*/*.json"))}


class HopTests(Project):
    def test_the_hops_come_in_the_order_the_value_travels_with_their_values(self) -> None:
        done = run_tool(self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "wrote reports/transcripts/chat-stdin.md and .json (8 hops)")
        _, document = self.report()
        hops = document["hops"]
        self.assertEqual(
            [(h["n"], h["kind"], h["where"]) for h in hops],
            [
                (1, "input", "chat (entry)"),
                (2, "source", "chat (entry) → history"),
                (3, "joint", "chat (entry) → planner (sealed)"),
                (4, "return", "planner (sealed) → chat (entry)"),
                (5, "joint", "chat (entry) → assistant (open)"),
                (6, "source", "assistant (open) → xai"),
                (7, "return", "assistant (open) → chat (entry)"),
                (8, "output", "chat (entry)"),
            ],
        )
        self.assertEqual([h["from"] for h in hops], ["chat", "chat", "chat", "planner", "chat", "assistant", "assistant", "chat"])
        self.assertEqual([h["to"] for h in hops], ["chat", "chat.history", "planner", "chat", "assistant", "assistant.xai", "chat", "chat"])
        self.assertEqual(hops[0]["value"], SAMPLE)
        self.assertEqual(hops[1]["request"], {"user": "ada"})
        self.assertEqual(hops[1]["value"], {"turns": 3})
        self.assertEqual(hops[2]["value"], {"goal": "ship"})
        self.assertEqual(hops[3]["value"], {"steps": ["ship-one", "ship-two"]})
        self.assertEqual(hops[4]["value"], {"steps": ["ship-one", "ship-two"], "user": "ada"})
        self.assertEqual(hops[5]["request"], {"steps": ["ship-one", "ship-two"], "user": "ada"})
        self.assertEqual(hops[5]["value"], {"text": "hello ada, ship-one, ship-two"})
        self.assertEqual(hops[6]["value"], {"text": "hello ada, ship-one, ship-two"})
        self.assertEqual(hops[7]["value"], {"greeting": "hello ada, ship-one, ship-two", "steps": ["ship-one", "ship-two"], "turns": 3})

    def test_how_it_got_here_names_the_joint_policy_pin_follow_and_the_sources_epoch_and_label(self) -> None:
        run_tool(self.root)
        _, document = self.report()
        how = [h["how"] for h in document["hops"]]
        self.assertEqual(
            how,
            [
                "sample input",
                "source epoch 1 label store=v1",
                "joint from chat, eventual, pin v1",
                "returned",
                "joint from chat, orchestrated, pin v2, follows xai",
                "source epoch 2 label model=grok-4",
                "returned",
                "output",
            ],
        )

    def test_each_source_hop_names_the_take_file_it_used_and_the_others_name_none(self) -> None:
        run_tool(self.root)
        _, document = self.report()
        self.assertEqual(
            [h["take"] for h in document["hops"]],
            [None, "bricks/chat/input/tapes/history/turns.json", None, None, None, "bricks/assistant/input/tapes/xai/reply.json", None, None],
        )
        for hop in document["hops"]:
            if hop["take"]:
                self.assertTrue((self.root / hop["take"]).is_file())

    def test_the_shapes_of_every_brick_touched_are_in_the_json(self) -> None:
        run_tool(self.root)
        _, document = self.report()
        self.assertEqual(document["shapes"], {"assistant": "open", "chat": "entry", "planner": "sealed"})
        self.assertEqual((document["brick"], document["name"], document["mode"]), ("chat", "stdin", {"fresh": [], "record": []}))

    def test_replay_is_the_default_and_changes_no_take(self) -> None:
        before = self.takes()
        run_tool(self.root)
        self.assertEqual(before, self.takes())
        ledger = [p.name for p in (self.root / "bricks/chat/runner/ledger").glob("*.json")]
        self.assertTrue(ledger and all(name.endswith(".local.json") for name in ledger), ledger)

    def test_the_report_has_no_run_id_timestamp_or_local_path(self) -> None:
        run_tool(self.root)
        markdown, document = self.report()
        text = markdown + json.dumps(document)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotRegex(text, r"\d{8}-\d{6}-\d{6}")
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")

    def test_the_input_file_and_name_decide_the_predictable_path(self) -> None:
        sample = self.tmp / "Order 7.json"
        sample.write_text(json.dumps(SAMPLE), encoding="utf-8")
        done = run_tool(self.root, "--input", str(sample))
        self.assertIn("reports/transcripts/chat-order-7.md", done.stdout)
        self.assertTrue((self.root / "reports/transcripts/chat-order-7.json").is_file())
        run_tool(self.root, "--name", "Second One", "--out", str(self.tmp / "elsewhere"))
        self.assertTrue((self.tmp / "elsewhere/chat-second-one.md").is_file())


class ModeTests(Project):
    def test_fresh_goes_to_the_named_source_only_and_says_no_take_was_used(self) -> None:
        before = self.takes()
        done = run_tool(self.root, "--fresh", "assistant.xai")
        self.assertEqual(done.returncode, 0, done.stderr)
        markdown, document = self.report()
        sources = [h for h in document["hops"] if h["kind"] == "source"]
        self.assertEqual([h["take"] for h in sources], ["bricks/chat/input/tapes/history/turns.json", None])
        self.assertIn("fresh (no take used)", sources[1]["how"])
        self.assertEqual(document["mode"], {"fresh": ["assistant.xai"], "record": []})
        self.assertIn("- Mode: fresh: assistant.xai (everything else replayed)", markdown)
        self.assertEqual(before, self.takes())

    def test_record_rewrites_the_named_take_and_names_it(self) -> None:
        (self.root / "bricks/assistant/input/tapes/xai/reply.json").unlink()
        done = run_tool(self.root, "--record", "assistant.xai")
        self.assertEqual(done.returncode, 0, done.stderr)
        _, document = self.report()
        source = [h for h in document["hops"] if h["to"] == "assistant.xai"][0]
        self.assertEqual(source["take"], "bricks/assistant/input/tapes/xai/reply.json")
        self.assertIn("recorded", source["how"])
        self.assertTrue((self.root / source["take"]).is_file())
        self.assertEqual(document["mode"], {"fresh": [], "record": ["assistant.xai"]})

    def test_an_unknown_port_is_an_error_and_a_report_of_the_failure_is_still_written(self) -> None:
        done = run_tool(self.root, "--fresh", "nothing")
        self.assertEqual(done.returncode, 1)
        self.assertIn("error: the run failed: ValueError: fresh= names 'nothing'", done.stderr)
        _, document = self.report()
        self.assertEqual(document["hops"], [])


class FailureTests(Project):
    def test_a_missing_take_is_reported_at_the_hop_where_it_failed(self) -> None:
        (self.root / "bricks/assistant/input/tapes/xai/reply.json").unlink()
        done = run_tool(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("error: the run failed: TapeError: take does not exist", done.stderr)
        _, document = self.report()
        kinds = [h["kind"] for h in document["hops"]]
        self.assertEqual(kinds, ["input", "source", "joint", "return", "joint", "source", "error", "error"])
        self.assertEqual(document["hops"][5]["value"]["error"]["type"], "TapeError")
        self.assertEqual(document["hops"][-1]["how"], "raised")
        self.assertEqual(document["hops"][-1]["where"], "chat (entry)")


class VariantTests(Project):
    def rewrite(self, brick: str, file: str, text: str) -> None:
        (self.root / "bricks" / brick / "input/adapters" / file).write_text(text, encoding="utf-8")

    def test_a_direct_load_of_a_take_is_a_hop_too(self) -> None:
        self.rewrite("chat", "history.py", (
            "from ..tape import load\n\n\ndef fetch(ctx, user):\n"
            "    return load('history', 'turns', {'user': user})['response']['turns']\n"
        ))
        run_tool(self.root)
        _, document = self.report()
        source = document["hops"][1]
        self.assertEqual((source["kind"], source["value"], source["take"]), ("source", {"turns": 3}, "bricks/chat/input/tapes/history/turns.json"))

    def test_every_operation_of_a_session_is_a_hop_with_its_window_and_take(self) -> None:
        self.rewrite("assistant", "xai.py", (
            "from ..tape import session\n\n\ndef complete(ctx, user, steps):\n"
            "    talk = session(ctx, 'xai', 'chat')\n    talk.take('w0', {'user': user})\n"
            "    first = talk.call('ask', {'steps': steps}, lambda: {'text': 'hi ' + user})\n"
            "    second = talk.call('more', {}, lambda: {'text': 'again'})\n    talk.close()\n"
            "    return first['text'] + '/' + second['text']\n"
        ))
        shutil.rmtree(self.root / "bricks/assistant/input/tapes/xai")
        sample_project.record_takes(self.root)
        done = run_tool(self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        _, document = self.report()
        sources = [h for h in document["hops"] if h["to"] == "assistant.xai"]
        self.assertEqual([h["value"] for h in sources], [{"text": "hi ada"}, {"text": "again"}])
        self.assertEqual([h["request"] for h in sources], [{"steps": ["ship-one", "ship-two"]}, {}])
        self.assertTrue(all(h["take"] == "bricks/assistant/input/tapes/xai/chat-w0.json" for h in sources))
        self.assertEqual(sources[0]["how"], "source epoch 2 label model=grok-4, session chat, window w0, ask")
        self.assertEqual(sources[1]["how"].rsplit(", ", 1)[1], "more")
        self.assertEqual(document["hops"][-1]["value"]["greeting"], "hi ada/again")


class ValueTests(Project):
    def test_values_are_shown_in_full_never_truncated(self) -> None:
        long = "x" * 700
        sample = {"user": "ada", "goal": long}
        run_tool(self.root, "--record", "*", sample=sample)
        done = run_tool(self.root, sample=sample)
        self.assertEqual(done.returncode, 0, done.stderr)
        markdown, document = self.report()
        self.assertEqual(document["hops"][0]["value"]["goal"], long)
        self.assertEqual(document["hops"][-1]["value"]["steps"], [long + "-one", long + "-two"])
        self.assertIn(long + "-two", markdown)
        self.assertNotIn("…", markdown)
        self.assertNotIn("sha256", markdown)

    def test_a_short_value_is_inline_and_a_long_one_is_pretty_printed_in_details(self) -> None:
        run_tool(self.root)
        markdown, _ = self.report()
        self.assertIn('| 1 | chat (entry) | sample input | none | <code>{"goal": "ship", "user": "ada"}</code> |', markdown)
        self.assertIn('<details><summary>8 lines</summary><pre>{&#10;  "greeting": "hello ada, ship-one, ship-two",&#10;  "steps": [&#10;    "ship-one",&#10;    "ship-two"&#10;  ],&#10;  "turns": 3&#10;}</pre></details>', markdown)

    def test_keys_are_in_a_fixed_order_whatever_order_the_value_was_built_in(self) -> None:
        self.assertEqual(
            transcript_brick.value_cell({"b": 1, "a": 2}),
            '<code>{"a": 2, "b": 1}</code>',
        )
        self.assertEqual(transcript_brick.plain({"z": (1, 2), "y": b"\x00\x01"}), {"z": [1, 2], "y": {"base64": "AAE="}})

    def test_html_and_table_characters_in_a_value_cannot_break_the_row(self) -> None:
        cell = transcript_brick.value_cell({"k": "a|b <i>&</i>"})
        self.assertEqual(cell, '<code>{"k": "a&#124;b &lt;i&gt;&amp;&lt;/i&gt;"}</code>')
        self.assertNotIn("|", cell)
        self.assertNotIn("\n", transcript_brick.value_cell({"k": "line\nbreak" * 20}))


class DiagramTests(Project):
    def test_the_sequence_diagram_has_a_participant_per_brick_in_first_touched_order_then_per_source(self) -> None:
        run_tool(self.root)
        markdown, _ = self.report()
        diagram = markdown.split("```mermaid\n", 1)[1].split("\n```", 1)[0]
        self.assertEqual(
            diagram.splitlines(),
            [
                "sequenceDiagram",
                "    participant p1 as chat (entry)",
                "    participant p2 as planner (sealed)",
                "    participant p3 as assistant (open)",
                "    participant s1 as chat.history (source)",
                "    participant s2 as assistant.xai (source)",
                "    Note over p1: 1 sample input",
                "    p1->>s1: 2 history: epoch 1 label store=v1, take bricks/chat/input/tapes/history/turns.json",
                "    p1->>p2: 3 joint planner: eventual, pin v1",
                "    p2-->>p1: 4 returned",
                "    p1->>p3: 5 joint assistant: orchestrated, pin v2, follows xai",
                "    p3->>s2: 6 xai: epoch 2 label model=grok-4, take bricks/assistant/input/tapes/xai/reply.json",
                "    p3-->>p1: 7 returned",
                "    Note over p1: 8 output",
            ],
        )

    def test_the_hop_table_has_the_agreed_columns(self) -> None:
        run_tool(self.root)
        markdown, _ = self.report()
        self.assertIn("| # | where | how it got here | tape take | value |\n|---|---|---|---|---|\n", markdown)
        self.assertEqual(sum(1 for line in markdown.splitlines() if line.startswith("| ") and line[2].isdigit()), 8)

    def test_participants_labels_and_messages_are_escaped(self) -> None:
        hops = [
            {"n": 1, "kind": "input", "where": "", "from": "a;b", "to": "a;b", "how": 'sample "input"; #1', "take": None, "value": {}},
            {"n": 2, "kind": "source", "where": "", "from": "a;b", "to": "a;b.p<q>", "how": "source epoch 1 label k=v|w", "take": "t;1/x.json", "value": {}},
        ]
        diagram = transcript_brick.sequence(hops, {"a;b": "open"})
        self.assertIn("participant p1 as a#59;b (open)", diagram)
        self.assertIn("participant s1 as a#59;b.p#lt;q#gt; (source)", diagram)
        self.assertIn("Note over p1: 1 sample #quot;input#quot;#59; #35;1", diagram)
        self.assertIn("take t#59;1/x.json", diagram)
        self.assertIn("k=v#124;w", diagram)
        for line in diagram.splitlines()[1:]:
            self.assertEqual(line.count(";"), line.count("#59;") + sum(1 for part in line.split("#") if part.startswith(("quot;", "lt;", "gt;", "124;", "35;"))))


class DeterminismTests(Project):
    def test_running_twice_gives_identical_bytes_for_both_files(self) -> None:
        first, second = self.tmp / "one", self.tmp / "two"
        run_tool(self.root, "--out", str(first))
        run_tool(self.root, "--out", str(second))
        for name in ("chat-stdin.md", "chat-stdin.json"):
            self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_a_copy_of_the_project_at_another_path_gives_the_same_bytes(self) -> None:
        copy = self.tmp / "deep" / "copy"
        shutil.copytree(self.root, copy, ignore=shutil.ignore_patterns("reports", "__pycache__"))
        run_tool(self.root)
        run_tool(copy)
        for name in ("chat-stdin.md", "chat-stdin.json"):
            self.assertEqual((self.root / "reports/transcripts" / name).read_bytes(), (copy / "reports/transcripts" / name).read_bytes(), name)

    def test_the_sample_report_is_exactly_this(self) -> None:
        run_tool(self.root, "--name", "ada")
        markdown, _ = self.report("ada")
        self.assertEqual(markdown, SAMPLE_REPORT)


SAMPLE_REPORT = '''# Transcript report: `chat` / `ada`

- Entry brick: `chat` (entry)
- Mode: replay (every source answered from its tape; nothing went to a live source)
- Hops: 8

## Sequence

```mermaid
sequenceDiagram
    participant p1 as chat (entry)
    participant p2 as planner (sealed)
    participant p3 as assistant (open)
    participant s1 as chat.history (source)
    participant s2 as assistant.xai (source)
    Note over p1: 1 sample input
    p1->>s1: 2 history: epoch 1 label store=v1, take bricks/chat/input/tapes/history/turns.json
    p1->>p2: 3 joint planner: eventual, pin v1
    p2-->>p1: 4 returned
    p1->>p3: 5 joint assistant: orchestrated, pin v2, follows xai
    p3->>s2: 6 xai: epoch 2 label model=grok-4, take bricks/assistant/input/tapes/xai/reply.json
    p3-->>p1: 7 returned
    Note over p1: 8 output
```

## Hops

| # | where | how it got here | tape take | value |
|---|---|---|---|---|
| 1 | chat (entry) | sample input | none | <code>{"goal": "ship", "user": "ada"}</code> |
| 2 | chat (entry) → history | source epoch 1 label store=v1 | <code>bricks/chat/input/tapes/history/turns.json</code> | request: <code>{"user": "ada"}</code><br>response: <code>{"turns": 3}</code> |
| 3 | chat (entry) → planner (sealed) | joint from chat, eventual, pin v1 | none | <code>{"goal": "ship"}</code> |
| 4 | planner (sealed) → chat (entry) | returned | none | <code>{"steps": ["ship-one", "ship-two"]}</code> |
| 5 | chat (entry) → assistant (open) | joint from chat, orchestrated, pin v2, follows xai | none | <code>{"steps": ["ship-one", "ship-two"], "user": "ada"}</code> |
| 6 | assistant (open) → xai | source epoch 2 label model=grok-4 | <code>bricks/assistant/input/tapes/xai/reply.json</code> | request: <code>{"steps": ["ship-one", "ship-two"], "user": "ada"}</code><br>response: <code>{"text": "hello ada, ship-one, ship-two"}</code> |
| 7 | assistant (open) → chat (entry) | returned | none | <code>{"text": "hello ada, ship-one, ship-two"}</code> |
| 8 | chat (entry) | output | none | <details><summary>8 lines</summary><pre>{&#10;  "greeting": "hello ada, ship-one, ship-two",&#10;  "steps": [&#10;    "ship-one",&#10;    "ship-two"&#10;  ],&#10;  "turns": 3&#10;}</pre></details> |
'''


class CommandTests(Project):
    def test_an_unknown_brick_and_bad_input_are_usage_errors(self) -> None:
        done = subprocess.run([sys.executable, TOOL, "nothing", "--root", str(self.root)], input="{}", capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("no brick named 'nothing'", done.stderr)
        done = subprocess.run([sys.executable, TOOL, "chat", "--root", str(self.root)], input="{nope", capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("cannot read the JSON input", done.stderr)

    def test_the_tool_does_not_leave_the_project_in_a_state_the_lint_rejects(self) -> None:
        from tools import lint_bricks

        run_tool(self.root)
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))


if __name__ == "__main__":
    unittest.main()
