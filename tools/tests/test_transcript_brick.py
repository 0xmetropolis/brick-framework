"""Tests for tools/transcript_brick.py: samples of the payloads a project passes around, grouped by brick."""

from __future__ import annotations

import json
from pathlib import Path
import random
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


def write_take(root: Path, brick: str, source: str, take: str, request: dict, **result: object) -> Path:
    record = {"epoch": 1, "recorded_in": "r1", "request": request, "schema_version": 2, "source": source, "take": take, **result}
    path = root / "bricks" / brick / "input/tapes" / source / f"{take}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


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

    def brick(self, document: dict, name: str) -> dict:
        return next(brick for brick in document["bricks"] if brick["name"] == name)

    def takes(self) -> dict[str, bytes]:
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in sorted(self.root.rglob("tapes/*/*.json"))}


class GroupingTests(Project):
    def test_there_is_one_section_per_brick_in_name_order_with_its_shape(self) -> None:
        done = run_tool(self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "wrote reports/transcripts/chat-stdin.md and .json (3 bricks)")
        markdown, document = self.report()
        self.assertEqual([b["name"] for b in document["bricks"]], ["assistant", "chat", "planner"])
        self.assertEqual([b["shape"] for b in document["bricks"]], ["open", "entry", "sealed"])
        self.assertEqual([line for line in markdown.splitlines() if line.startswith("## ")], ["## assistant (open)", "## chat (entry)", "## planner (sealed)"])
        for heading in ("### Run", "### Joints", "### Ports"):
            self.assertEqual(markdown.count(heading), 3)

    def test_each_brick_shows_its_own_run_call_with_input_and_output_and_who_called_it(self) -> None:
        run_tool(self.root)
        _, document = self.report()
        chat = self.brick(document, "chat")["run"]
        self.assertEqual((chat["calls"], [s["label"] for s in chat["samples"]], chat["samples"][0]["from"]), (1, ["only"], "sample input"))
        self.assertEqual(chat["samples"][0]["input"]["value"], SAMPLE)
        self.assertEqual(chat["samples"][0]["output"]["value"], {"greeting": "hello ada, ship-one, ship-two", "steps": ["ship-one", "ship-two"], "turns": 3})
        planner = self.brick(document, "planner")["run"]["samples"][0]
        self.assertEqual((planner["from"], planner["input"]["value"], planner["output"]["value"]), ("chat", {"goal": "ship"}, {"steps": ["ship-one", "ship-two"]}))
        assistant = self.brick(document, "assistant")["run"]["samples"][0]
        self.assertEqual(assistant["from"], "chat")
        self.assertEqual(assistant["input"]["value"], {"steps": ["ship-one", "ship-two"], "user": "ada"})

    def test_the_joint_traffic_is_what_crossed_and_what_came_back_and_equals_the_siblings_run_call(self) -> None:
        run_tool(self.root)
        _, document = self.report()
        joints = {j["name"]: j for j in self.brick(document, "chat")["joints"]}
        self.assertEqual(sorted(joints), ["assistant", "planner"])
        self.assertEqual((joints["assistant"]["policy"], joints["assistant"]["pin"], joints["assistant"]["follow"]), ("orchestrated", 2, ["xai"]))
        self.assertEqual((joints["planner"]["policy"], joints["planner"]["pin"], joints["planner"]["follow"]), ("eventual", 1, []))
        for name, joint in joints.items():
            self.assertEqual(joint["crossings"], 1)
            callee = self.brick(document, name)["run"]["samples"][0]
            crossing = joint["samples"][0]
            self.assertEqual((crossing["input"], crossing["output"]), (callee["input"], callee["output"]))

    def test_each_source_port_shows_requests_and_responses_from_the_takes_on_its_tape(self) -> None:
        run_tool(self.root)
        _, document = self.report()
        (history,) = self.brick(document, "chat")["ports"]
        self.assertEqual((history["name"], history["epoch"], history["label"], history["followable"]), ("history", 1, {"store": "v1"}, False))
        self.assertEqual((history["takes"], history["unreadable"]), (1, 0))
        sample = history["samples"][0]
        self.assertEqual(sample["take"], "bricks/chat/input/tapes/history/turns.json")
        self.assertEqual(sample["request"]["value"], {"label": {"store": "v1"}, "user": "ada"})
        self.assertEqual(sample["response"]["value"], {"turns": 3})
        (xai,) = self.brick(document, "assistant")["ports"]
        self.assertEqual((xai["epoch"], xai["followable"]), (2, True))

    def test_a_brick_the_sample_does_not_reach_still_shows_its_tape_and_says_it_was_not_called(self) -> None:
        idle = sample_project.brick(
            self.root, "idle", sample_project.ASSISTANT_CONTRACT.replace("CONTRACT_VERSION = 2", "CONTRACT_VERSION = 1"),
            sample_project.ASSISTANT_LOGIC, {"xai.py": sample_project.ASSISTANT_XAI},
        )
        write_take(self.root, "idle", "xai", "reply", {"label": {"model": "grok-4"}, "q": 1}, response={"idle": True})
        run_tool(self.root)
        markdown, document = self.report()
        brick = self.brick(document, "idle")
        self.assertEqual(brick["run"], {"calls": 0, "samples": []})
        self.assertEqual(brick["ports"][0]["samples"][0]["response"]["value"], {"idle": True})
        section = markdown.split("## idle (open)", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Not called by this sample.", section)
        self.assertIn("**response** (13 bytes)", section)

    def test_a_joint_the_sample_never_crossed_says_so(self) -> None:
        run_tool(self.root, sample=SAMPLE)
        markdown, _ = self.report()
        self.assertNotIn("Not crossed by this sample.", markdown)
        # A run that fails before the joints are reached leaves them uncrossed.
        broken = self.root / "bricks/chat/src/logic.py"
        broken.write_text(broken.read_text(encoding="utf-8").replace("    turns = history.fetch", "    raise KeyError('early')\n    turns = history.fetch"), encoding="utf-8")
        run_tool(self.root)
        markdown, document = self.report()
        self.assertEqual(markdown.count("Not crossed by this sample."), 2)
        self.assertEqual([j["crossings"] for j in self.brick(document, "chat")["joints"]], [0, 0])

    def test_the_report_has_no_flow_diagram_and_no_hop_table(self) -> None:
        run_tool(self.root)
        markdown, document = self.report()
        for gone in ("sequenceDiagram", "mermaid", "| # |", "how it got here", "participant"):
            self.assertNotIn(gone, markdown)
        self.assertNotIn("hops", document)
        for name in ("sequence", "value_cell", "markdown_hops", "Hop"):
            self.assertFalse(hasattr(transcript_brick, name), name)
        self.assertNotIn("patch_tape", dir(transcript_brick.Tracer))

    def test_replay_changes_no_take_and_leaves_only_a_local_ledger_entry(self) -> None:
        before = self.takes()
        run_tool(self.root)
        self.assertEqual(before, self.takes())
        names = [p.name for p in (self.root / "bricks/chat/runner/ledger").glob("*.json")]
        self.assertTrue(names and all(name.endswith(".local.json") for name in names), names)


class SelectionTests(unittest.TestCase):
    def candidates(self, sizes: list[int], texts: list[str] | None = None) -> list[dict]:
        return [{"size": size, "text": (texts[i] if texts else str(i)), "id": i} for i, size in enumerate(sizes)]

    def picked(self, items: list[dict]) -> list[tuple[str, int]]:
        return [(label, item["id"]) for label, item in transcript_brick.choose(items)]

    def test_none_one_two_and_three_candidates(self) -> None:
        self.assertEqual(self.picked([]), [])
        self.assertEqual(self.picked(self.candidates([5])), [("only", 0)])
        self.assertEqual(self.picked(self.candidates([9, 2])), [("smallest", 1), ("largest", 0)])
        self.assertEqual(self.picked(self.candidates([9, 2, 5])), [("smallest", 1), ("typical", 2), ("largest", 0)])

    def test_with_more_than_three_it_is_the_smallest_the_lower_median_and_the_largest(self) -> None:
        self.assertEqual(self.picked(self.candidates([40, 10, 30, 20])), [("smallest", 1), ("typical", 3), ("largest", 0)])
        self.assertEqual(self.picked(self.candidates([50, 10, 30, 20, 40])), [("smallest", 1), ("typical", 2), ("largest", 0)])
        sizes = list(range(100, 0, -1))
        self.assertEqual([item["size"] for _, item in transcript_brick.choose(self.candidates(sizes))], [1, 50, 100])

    def test_ties_are_broken_by_text_then_by_order_and_the_input_order_never_matters(self) -> None:
        items = self.candidates([5, 5, 5, 5, 5, 5, 5], texts=["g", "c", "a", "f", "b", "e", "d"])
        expected = [(label, items[i]["text"]) for label, i in self.picked(items)]
        self.assertEqual(expected, [("smallest", "a"), ("typical", "d"), ("largest", "g")])
        for seed in range(20):
            shuffled = items[:]
            random.Random(seed).shuffle(shuffled)
            self.assertEqual([(l, item["text"]) for l, item in transcript_brick.choose(shuffled)], expected)
        same = self.candidates([5, 5, 5, 5], texts=["x"] * 4)
        self.assertEqual(self.picked(same), [("smallest", 0), ("typical", 1), ("largest", 3)])

    def test_the_size_is_the_canonical_json_in_bytes(self) -> None:
        self.assertEqual(transcript_brick.size_of({"b": 1, "a": "é"}), len('{"a":"é","b":1}'.encode("utf-8")))
        self.assertEqual(transcript_brick.canonical({"b": [1, 2], "a": None}), '{"a":null,"b":[1,2]}')


class TrimTests(unittest.TestCase):
    def test_a_small_payload_is_whole_with_its_size(self) -> None:
        shown = transcript_brick.present({"a": 1})
        self.assertEqual(shown, {"bytes": 7, "value": {"a": 1}})

    def test_the_limit_is_in_bytes_of_canonical_json(self) -> None:
        exact = {"k": "x" * (transcript_brick.TRIM_BYTES - len('{"k":""}'))}
        self.assertEqual(transcript_brick.size_of(exact), transcript_brick.TRIM_BYTES)
        self.assertIn("value", transcript_brick.present(exact))
        over = {"k": "x" * (transcript_brick.TRIM_BYTES - len('{"k":""}') + 1)}
        self.assertIn("top_level_keys", transcript_brick.present(over))

    def test_a_big_object_becomes_its_top_level_keys_each_with_type_size_and_one_sample(self) -> None:
        big = {
            "map": [[row, col] for row in range(40) for col in range(10)],
            "log": ["turn 1 began"] * 500,
            "state": {"day": 3, "units": list(range(300))},
            "name": "x" * 2000,
            "tick": 41,
            "empty": [],
            "none": None,
            "flag": True,
            "nested": {"inner": {"a": 1}},
            "rows": [{"id": 1}] * 400,
        }
        shown = transcript_brick.present(big)
        self.assertEqual(shown["bytes"], transcript_brick.size_of(big))
        keys = shown["top_level_keys"]
        self.assertEqual(list(keys), sorted(big))
        self.assertEqual(keys["map"], {"type": "list", "size": 400, "sample": "list of 2 items"})
        self.assertEqual(keys["log"], {"type": "list", "size": 500, "sample": '"turn 1 began"'})
        self.assertEqual(keys["state"], {"type": "object", "size": 2, "sample": '"day": 3'})
        self.assertEqual(keys["name"], {"type": "string", "sample": '"' + "x" * 40 + '…" (2000 chars)'})
        self.assertEqual(keys["tick"], {"type": "number", "sample": "41"})
        self.assertEqual(keys["empty"], {"type": "list", "size": 0, "sample": "[]"})
        self.assertEqual(keys["none"], {"type": "null", "sample": "null"})
        self.assertEqual(keys["flag"], {"type": "boolean", "sample": "true"})
        self.assertEqual(keys["nested"], {"type": "object", "size": 1, "sample": '"inner": object of 1 keys'})
        self.assertEqual(keys["rows"], {"type": "list", "size": 400, "sample": "object of 1 keys"})
        for entry in keys.values():
            self.assertLess(len(entry["sample"]), 80)

    def test_a_big_payload_that_is_not_an_object_is_described_as_a_whole(self) -> None:
        shown = transcript_brick.present(list(range(2000)))
        self.assertEqual(shown["top_level_keys"], {"(the whole value)": {"type": "list", "size": 2000, "sample": "0"}})
        shown = transcript_brick.present("y" * 3000)
        self.assertEqual(shown["top_level_keys"]["(the whole value)"]["type"], "string")

    def test_bytes_become_a_base64_object_so_nothing_is_lost_or_unprintable(self) -> None:
        self.assertEqual(transcript_brick.plain({"z": (1, 2), "y": b"\x00\x01"}), {"z": [1, 2], "y": {"base64": "AAE="}})


class BigPayloadTests(Project):
    def big_state(self) -> dict:
        return {
            "map": [[row, col, "grass" if (row + col) % 3 else "rock"] for row in range(300) for col in range(100)],
            "units": [{"id": i, "name": f"unit-{i}", "hp": 100 - i % 7, "pos": [i % 50, i // 50]} for i in range(2500)],
            "log": [f"turn {i} began and ended without incident" for i in range(3000)],
            "day": 41,
            "seed": "a" * 5000,
        }

    def test_a_game_sized_payload_is_trimmed_to_its_keys_and_the_report_stays_small(self) -> None:
        state = self.big_state()
        write_take(self.root, "chat", "history", "turns", {"label": {"store": "v1"}, "user": "ada"}, response={"turns": 3, "state": state})
        full = transcript_brick.size_of({"request": {"label": {"store": "v1"}, "user": "ada"}, "response": {"turns": 3, "state": state}})
        self.assertGreater(full, 400_000)
        blob = {"user": "ada", "goal": "ship", "blob": state}
        done = run_tool(self.root, sample=blob)
        self.assertEqual(done.returncode, 0, done.stderr)
        markdown, document = self.report()
        json_text = (self.root / "reports/transcripts/chat-stdin.json").read_text(encoding="utf-8")
        self.assertLess(len(markdown.encode("utf-8")) * 40, full)
        self.assertLess(len(json_text.encode("utf-8")) * 40, full)
        history = self.brick(document, "chat")["ports"][0]["samples"][0]
        self.assertEqual(history["response"]["bytes"], transcript_brick.size_of({"turns": 3, "state": state}))
        self.assertEqual(sorted(history["response"]["top_level_keys"]), ["state", "turns"])
        self.assertEqual(history["response"]["top_level_keys"]["state"], {"type": "object", "size": 5, "sample": '"day": 41'})
        run_input = self.brick(document, "chat")["run"]["samples"][0]["input"]
        self.assertEqual(sorted(run_input["top_level_keys"]), ["blob", "goal", "user"])
        self.assertIn("trimmed to its top-level keys (full size", markdown)
        self.assertNotIn("unit-2499", markdown)
        self.assertNotIn("a" * 100, markdown)
        self.assertIn("state  object of 5 keys, e.g. \"day\": 41", markdown)

    def test_the_size_comparison_is_stated_in_the_report_for_every_trimmed_payload(self) -> None:
        state = self.big_state()
        write_take(self.root, "chat", "history", "turns", {"label": {"store": "v1"}, "user": "ada"}, response={"turns": 3, "state": state})
        run_tool(self.root)
        markdown, document = self.report()
        full = self.brick(document, "chat")["ports"][0]["samples"][0]["response"]["bytes"]
        self.assertIn(f"(full size {full:,} bytes)", markdown)
        self.assertIn(f"**response** ({full:,} bytes)", markdown)


class TapeSampleTests(Project):
    REQUEST = {"label": {"store": "v1"}}

    def fill(self, sizes: dict[str, int]) -> None:
        for take, size in sizes.items():
            write_take(self.root, "chat", "history", take, {**self.REQUEST, "id": take}, response={"pad": "p" * size})

    def test_three_samples_are_chosen_from_many_takes_by_size_and_untouched_takes_count(self) -> None:
        self.fill({"a": 10, "b": 400, "c": 30, "d": 200, "e": 20, "f": 5})
        run_tool(self.root)
        _, document = self.report()
        history = self.brick(document, "chat")["ports"][0]
        self.assertEqual(history["takes"], 7)
        self.assertEqual([s["label"] for s in history["samples"]], ["smallest", "typical", "largest"])
        takes = [Path(s["take"]).stem for s in history["samples"]]
        sizes = sorted(
            (transcript_brick.size_of({"request": {**self.REQUEST, "id": t}, "response": {"pad": "p" * n}}), t)
            for t, n in {"a": 10, "b": 400, "c": 30, "d": 200, "e": 20, "f": 5}.items()
        )
        sizes.append((transcript_brick.size_of({"request": {"label": {"store": "v1"}, "user": "ada"}, "response": {"turns": 3}}), "turns"))
        ordered = [t for _, t in sorted(sizes)]
        self.assertEqual(takes, [ordered[0], ordered[(len(ordered) - 1) // 2], ordered[-1]])
        self.assertEqual(takes[-1], "b")

    def test_the_choice_does_not_depend_on_the_files_order_on_disk_or_the_input(self) -> None:
        self.fill({"a": 10, "b": 400, "c": 30, "d": 200})
        run_tool(self.root)
        first = self.report()[1]["bricks"]
        shutil.rmtree(self.root / "reports")
        other = {"user": "ada", "goal": "ship"}
        run_tool(self.root, sample=other)
        self.assertEqual(first, self.report()[1]["bricks"])

    def test_an_error_take_is_shown_as_an_error_and_a_session_take_whole_when_small(self) -> None:
        write_take(self.root, "chat", "history", "bad", {**self.REQUEST, "id": "bad"}, error={"type": "OSError"})
        write_take(
            self.root, "chat", "history", "year-w0", {**self.REQUEST, "window": "w0"},
            response={"transcript": [{"op": "ask", "request": {"q": 1}, "response": {"a": 2}}, {"op": "more", "request": {}, "error": {"type": "OSError"}}]},
        )
        run_tool(self.root)
        _, document = self.report()
        samples = {Path(s["take"]).stem: s for s in self.brick(document, "chat")["ports"][0]["samples"]}
        self.assertEqual(samples["bad"]["error"]["value"], {"type": "OSError"})
        self.assertNotIn("response", samples["bad"])
        self.assertEqual(samples["year-w0"]["response"]["value"]["transcript"][1]["error"], {"type": "OSError"})

    def test_files_that_are_not_takes_are_counted_and_not_shown(self) -> None:
        folder = self.root / "bricks/chat/input/tapes/history"
        (folder / "broken.json").write_text("{nope", encoding="utf-8")
        (folder / "other.json").write_text('{"schema_version": 2}\n', encoding="utf-8")
        (folder / "both.json").write_text('{"request": {}, "response": 1, "error": {}}\n', encoding="utf-8")
        run_tool(self.root)
        markdown, document = self.report()
        history = self.brick(document, "chat")["ports"][0]
        self.assertEqual((history["takes"], history["unreadable"]), (1, 3))
        self.assertIn("3 files in the folder are not a readable take.", markdown)

    def test_a_port_with_no_takes_says_so(self) -> None:
        shutil.rmtree(self.root / "bricks/assistant/input/tapes/xai")
        run_tool(self.root)
        _, document = self.report()
        xai = self.brick(document, "assistant")["ports"][0]
        self.assertEqual((xai["takes"], xai["samples"]), (0, []))


class FailureAndModeTests(Project):
    def test_a_failed_run_is_still_reported_with_the_calls_it_made_and_exits_one(self) -> None:
        (self.root / "bricks/assistant/input/tapes/xai/reply.json").unlink()
        done = run_tool(self.root)
        self.assertEqual(done.returncode, 1)
        self.assertIn("error: the run failed: TapeError: take does not exist", done.stderr)
        _, document = self.report()
        self.assertTrue(document["error"].startswith("TapeError: take does not exist"))
        assistant = self.brick(document, "assistant")["run"]["samples"][0]
        self.assertEqual(assistant["error"]["value"]["type"], "TapeError")
        self.assertNotIn("output", assistant)
        chat = self.brick(document, "chat")["run"]["samples"][0]
        self.assertEqual(chat["error"]["value"]["type"], "TapeError")

    def test_fresh_and_record_are_passed_to_the_run_and_named_in_the_report(self) -> None:
        done = run_tool(self.root, "--fresh", "assistant.xai")
        self.assertEqual(done.returncode, 0, done.stderr)
        markdown, document = self.report()
        self.assertEqual(document["mode"], {"fresh": ["assistant.xai"], "record": []})
        self.assertIn("- Mode: fresh: assistant.xai (everything else replayed)", markdown)
        (self.root / "bricks/assistant/input/tapes/xai/reply.json").unlink()
        run_tool(self.root, "--record", "assistant.xai")
        _, document = self.report()
        self.assertEqual(document["mode"], {"fresh": [], "record": ["assistant.xai"]})
        self.assertTrue((self.root / "bricks/assistant/input/tapes/xai/reply.json").is_file())
        self.assertEqual(self.brick(document, "assistant")["ports"][0]["takes"], 1)

    def test_replay_is_stated_when_nothing_was_asked_for(self) -> None:
        run_tool(self.root)
        markdown, document = self.report()
        self.assertEqual(document["mode"], {"fresh": [], "record": []})
        self.assertIn("- Mode: replay (every source answered from its tape; nothing went to a live source)", markdown)

    def test_an_unknown_port_is_an_error_and_a_report_of_it_is_still_written(self) -> None:
        done = run_tool(self.root, "--fresh", "nothing")
        self.assertEqual(done.returncode, 1)
        self.assertIn("error: the run failed: ValueError: fresh= names 'nothing'", done.stderr)
        _, document = self.report()
        self.assertEqual(self.brick(document, "chat")["run"]["calls"], 0)


class PathAndCommandTests(Project):
    def test_the_input_file_and_name_decide_the_predictable_path(self) -> None:
        sample = self.tmp / "Order 7.json"
        sample.write_text(json.dumps(SAMPLE), encoding="utf-8")
        done = run_tool(self.root, "--input", str(sample))
        self.assertIn("reports/transcripts/chat-order-7.md", done.stdout)
        self.assertTrue((self.root / "reports/transcripts/chat-order-7.json").is_file())
        run_tool(self.root, "--name", "Second One", "--out", str(self.tmp / "elsewhere"))
        self.assertTrue((self.tmp / "elsewhere/chat-second-one.md").is_file())

    def test_an_unknown_brick_and_bad_input_are_usage_errors(self) -> None:
        done = subprocess.run([sys.executable, TOOL, "nothing", "--root", str(self.root)], input="{}", capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("no brick named 'nothing'", done.stderr)
        done = subprocess.run([sys.executable, TOOL, "chat", "--root", str(self.root)], input="{nope", capture_output=True, text=True)
        self.assertEqual(done.returncode, 2)
        self.assertIn("cannot read the JSON input", done.stderr)

    def test_the_project_still_lints_clean_after_a_report(self) -> None:
        from tools import lint_bricks

        run_tool(self.root)
        lint = lint_bricks.lint_repo(self.root)
        self.assertEqual((lint.errors, lint.warnings), ([], []))


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

    def test_the_report_has_no_run_id_timestamp_or_local_path(self) -> None:
        run_tool(self.root)
        markdown, document = self.report()
        text = markdown + json.dumps(document)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(str(self.tmp), text)
        self.assertNotRegex(text, r"\d{8}-\d{6}-\d{6}")
        self.assertNotRegex(text, r"\d{4}-\d{2}-\d{2}")

    def test_the_markdown_and_the_json_say_the_same_things(self) -> None:
        run_tool(self.root)
        markdown, document = self.report()
        for brick in document["bricks"]:
            self.assertIn(f"## {brick['name']} ({brick['shape']})", markdown)
            for port in brick["ports"]:
                for sample in port["samples"]:
                    self.assertIn(f"`{sample['take']}`", markdown)
        self.assertEqual(markdown.count("**input**"), 3)
        self.assertEqual(markdown.count("**crossed**"), 2)
        self.assertEqual(markdown.count("**request**"), 2)

    def test_the_sample_report_is_exactly_this(self) -> None:
        run_tool(self.root, "--name", "ada")
        markdown, _ = self.report("ada")
        self.assertEqual(markdown, (ROOT / "tools/tests/expected_transcript.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
