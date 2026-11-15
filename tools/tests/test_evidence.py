"""Tests for the boilerplate bricks/example_brick/input/evidence.py that every brick copies."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bricks.example_brick.input import evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(evidence, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def save(self, case: str = "day1", request: dict | None = None, **kwargs):
        return evidence.save_example(
            "feed", case, "run-1", request or {"day": 1, "mod": "v1"}, response=kwargs.get("response", {"ok": True})
        )

    def test_round_trip_and_provenance(self) -> None:
        self.save()
        record = evidence.load_example("feed", "day1", {"mod": "v1", "day": 1})
        self.assertEqual(record["response"], {"ok": True})
        self.assertEqual(evidence.provenance(record), {"recorded": True, "capture_run_id": "run-1"})

    def test_a_changed_request_says_which_field_changed(self) -> None:
        self.save()
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.load_example("feed", "day1", {"day": 1, "mod": "v2"})
        message = str(caught.exception)
        self.assertIn('mod: saved "v1", requested "v2"', message)
        self.assertIn("captured by run run-1", message)
        self.assertIn("re-record it with run(..., save=True)", message)

    def test_diff_covers_nested_lists_and_absent_fields(self) -> None:
        saved = {"a": {"b": [1, 2]}, "gone": 1}
        requested = {"a": {"b": [1, 3]}, "new": 2}
        diff = evidence.request_diff(saved, requested)
        self.assertIn("a.b[1]: saved 2, requested 3", diff)
        self.assertIn("gone: saved 1, requested absent", diff)
        self.assertIn("new: saved absent, requested 2", diff)

    def test_diff_is_bounded(self) -> None:
        saved = {str(i): i for i in range(20)}
        requested = {str(i): i + 1 for i in range(20)}
        self.assertEqual(len(evidence.request_diff(saved, requested)), 5)

    def test_a_missing_case_lists_the_cases_that_exist(self) -> None:
        self.save("day1")
        self.save("day2")
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.load_example("feed", "day9", {"day": 9})
        self.assertIn("cases saved for feed: day1, day2", str(caught.exception))
        self.assertIn("save=True", str(caught.exception))

    def test_a_missing_adapter_says_none(self) -> None:
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.load_example("nothing", "x", {})
        self.assertIn("cases saved for nothing: none", str(caught.exception))

    def test_oversize_evidence_says_what_to_do(self) -> None:
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.save_example("feed", "big", "run-1", {}, response={"x": "y" * 500}, max_bytes=100)
        message = str(caught.exception)
        for hint in ("projection", "cases per window", "limits.max_evidence_bytes"):
            self.assertIn(hint, message)

    def test_digest_is_stable_and_order_free(self) -> None:
        self.assertEqual(evidence.digest({"a": 1, "b": [2]}), evidence.digest({"b": [2], "a": 1}))
        self.assertNotEqual(evidence.digest({"a": 1}), evidence.digest({"a": 2}))
        self.assertTrue(evidence.digest({}).startswith("sha256:"))

    def test_the_file_stays_canonical(self) -> None:
        path = self.save()
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")


FP = {"game": "0.71", "protocol": "p1"}


def ctx(mode: str, run_id: str = "run-2", max_bytes: int | None = None) -> dict:
    config = {"limits": {"max_evidence_bytes": max_bytes}} if max_bytes else {}
    return {"mode": mode, "run_id": run_id, "config": config}


class ThroughTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(evidence, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = 0

    def live(self):
        self.calls += 1
        return {"answer": self.calls}

    def test_provenance_without_a_record_is_live(self) -> None:
        self.assertEqual(evidence.provenance(), {"recorded": False, "capture_run_id": None})

    def test_save_then_saved_round_trip(self) -> None:
        response, prov = evidence.through(ctx("save"), "feed", "c", {"q": 1}, self.live)
        self.assertEqual((response, prov), ({"answer": 1}, {"recorded": False, "capture_run_id": None}))
        again, prov = evidence.through(ctx("saved"), "feed", "c", {"q": 1}, self.live)
        self.assertEqual(again, {"answer": 1})
        self.assertEqual(prov, {"recorded": True, "capture_run_id": "run-2"})
        self.assertEqual(self.calls, 1)

    def test_fresh_calls_live_and_writes_nothing(self) -> None:
        evidence.through(ctx("fresh"), "feed", "c", {"q": 1}, self.live)
        with self.assertRaises(evidence.EvidenceError):
            evidence.through(ctx("saved"), "feed", "c", {"q": 1}, self.live)

    def test_saved_never_calls_live(self) -> None:
        with self.assertRaises(evidence.EvidenceError):
            evidence.through(ctx("saved"), "feed", "missing", {}, self.live)
        self.assertEqual(self.calls, 0)

    def test_a_failure_is_recorded_in_save_mode_and_raised_when_replayed(self) -> None:
        def boom():
            raise ValueError("no")

        with self.assertRaises(ValueError):
            evidence.through(ctx("save"), "feed", "c", {}, boom)
        with self.assertRaises(RuntimeError) as caught:
            evidence.through(ctx("saved"), "feed", "c", {}, boom)
        self.assertIn("saved adapter error: {'type': 'ValueError'}", str(caught.exception))

    def test_record_errors_false_leaves_a_good_example_alone_on_failure(self) -> None:
        evidence.through(ctx("save"), "feed", "c", {"q": 1}, lambda: {"a": 1})

        def boom():
            raise ValueError("transient")

        with self.assertRaises(ValueError):
            evidence.through(ctx("save"), "feed", "c", {"q": 1}, boom, record_errors=False)
        again, _ = evidence.through(ctx("saved"), "feed", "c", {"q": 1}, dict)
        self.assertEqual(again, {"a": 1})

    def test_the_default_still_records_a_failure(self) -> None:
        def boom():
            raise ValueError("x")

        with self.assertRaises(ValueError):
            evidence.through(ctx("save"), "feed", "c2", {"q": 1}, boom)
        with self.assertRaises(RuntimeError):
            evidence.through(ctx("saved"), "feed", "c2", {"q": 1}, dict)

    def test_the_size_limit_comes_from_the_run_context(self) -> None:
        with self.assertRaises(evidence.EvidenceError):
            evidence.through(ctx("save", max_bytes=100), "feed", "c", {}, lambda: {"x": "y" * 500})


class TranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(evidence, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, record=True) -> list:
        log = []
        script = evidence.Transcript(ctx("save"), "game", "year", record, fingerprint=FP)
        script.begin("w0", {"protocol": "p1", "after": "d0"})
        log.append(script.op("call", {"cmd": "state"}, lambda: {"day": 1}))
        log.append(script.op("advance", {"days": 1}, lambda: {"day": 2}))
        script.begin("w1", {"protocol": "p1", "after": "d1"})
        log.append(script.op("call", {"cmd": "state"}, lambda: {"day": 2}))
        script.end()
        return log

    def test_windows_are_one_example_each_and_replay_in_order(self) -> None:
        self.record()
        names = sorted(path.stem for path in evidence.DATA_DIR.glob("game/*.json"))
        self.assertEqual(names, ["year-w0", "year-w1"])
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        prov = script.begin("w0", {"protocol": "p1", "after": "d0"})
        self.assertEqual(prov["recorded"], True)
        self.assertEqual(script.op("call", {"cmd": "state"}, lambda: 1 / 0), {"day": 1})
        self.assertEqual(script.op("advance", {"days": 1}, lambda: 1 / 0), {"day": 2})

    def test_a_stale_window_is_found_alone_by_its_request(self) -> None:
        self.record()
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        with self.assertRaises(evidence.EvidenceError) as caught:
            script.begin("w1", {"protocol": "p2", "after": "d1"})
        self.assertIn('protocol: saved "p1", requested "p2"', str(caught.exception))

    def test_a_changed_operation_names_the_field_and_the_operation(self) -> None:
        self.record()
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        script.begin("w0", {"protocol": "p1", "after": "d0"})
        script.op("call", {"cmd": "state"}, lambda: 1 / 0)
        with self.assertRaises(evidence.EvidenceError) as caught:
            script.op("advance", {"days": 2}, lambda: 1 / 0)
        message = str(caught.exception)
        self.assertIn("mismatch at operation 2 (advance)", message)
        self.assertIn("days: saved 1, requested 2", message)
        self.assertIn("re-record the window with save", message)

    def test_a_different_kind_and_an_extra_operation_are_errors(self) -> None:
        self.record()
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        script.begin("w1", {"protocol": "p1", "after": "d1"})
        with self.assertRaises(evidence.EvidenceError) as kind:
            script.op("advance", {"days": 1}, lambda: 1 / 0)
        self.assertIn("recorded a call, requested a advance", str(kind.exception))
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        script.begin("w1", {"protocol": "p1", "after": "d1"})
        script.op("call", {"cmd": "state"}, lambda: 1 / 0)
        with self.assertRaises(evidence.EvidenceError) as extra:
            script.op("call", {"cmd": "state"}, lambda: 1 / 0)
        self.assertIn("ended after 1 operations", str(extra.exception))

    def test_only_chosen_windows_are_recorded(self) -> None:
        self.record(["w1"])
        self.assertEqual(sorted(path.stem for path in evidence.DATA_DIR.glob("game/*.json")), ["year-w1"])

    def test_fresh_records_nothing(self) -> None:
        script = evidence.Transcript(ctx("fresh"), "game", "year", fingerprint=FP)
        script.begin("w0", {})
        self.assertEqual(script.op("call", {}, lambda: {"ok": 1}), {"ok": 1})
        script.end()
        self.assertEqual(list(evidence.DATA_DIR.glob("game/*.json")), [])


class PassModeTests(unittest.TestCase):
    def test_saved_passes_nothing_and_lists_nothing(self) -> None:
        context = {"mode": "saved"}
        self.assertEqual(evidence.pass_mode(context, "game"), {"fresh": False, "save": False})
        self.assertNotIn("fresh_siblings", context)

    def test_fresh_passes_fresh_only_and_lists_the_sibling(self) -> None:
        context = {"mode": "fresh"}
        self.assertEqual(evidence.pass_mode(context, "game"), {"fresh": True, "save": False})
        self.assertEqual(context["fresh_siblings"], ["game"])

    def test_save_implies_fresh(self) -> None:
        context = {"mode": "save"}
        self.assertEqual(evidence.pass_mode(context, "game"), {"fresh": True, "save": True})
        self.assertEqual(context["fresh_siblings"], ["game"])

    def test_each_sibling_is_listed_once(self) -> None:
        context = {"mode": "fresh"}
        for name in ("game", "model", "game"):
            evidence.pass_mode(context, name)
        self.assertEqual(context["fresh_siblings"], ["game", "model"])


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(evidence, "DATA_DIR", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def record(self, fingerprint: dict) -> None:
        script = evidence.Transcript(ctx("save"), "game", "year", fingerprint=fingerprint)
        script.begin("w0", {"after": "d0"})
        script.op("call", {"cmd": "state"}, lambda: {"day": 1})
        script.end()

    def test_a_transcript_needs_a_fingerprint(self) -> None:
        with self.assertRaises(TypeError):
            evidence.Transcript(ctx("saved"), "game", "year")
        for empty in ({}, None, "v1"):
            with self.assertRaises(evidence.EvidenceError):
                evidence.Transcript(ctx("saved"), "game", "year", fingerprint=empty)

    def test_the_fingerprint_is_in_every_saved_window_request(self) -> None:
        self.record(FP)
        saved = json.loads((evidence.DATA_DIR / "game/year-w0.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"]["fingerprint"], FP)
        self.assertEqual(saved["request"]["window"], "w0")

    def test_replay_against_a_changed_fingerprint_is_a_clear_stale_error(self) -> None:
        self.record(FP)
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint={**FP, "game": "0.72"})
        with self.assertRaises(evidence.EvidenceError) as caught:
            script.begin("w0", {"after": "d0"})
        self.assertIn('fingerprint.game: saved "0.71", requested "0.72"', str(caught.exception))
        self.assertNotIsInstance(caught.exception, evidence.SourceMismatch)

    def test_replay_with_the_recorded_fingerprint_works(self) -> None:
        self.record(FP)
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        script.begin("w0", {"after": "d0"})
        self.assertEqual(script.op("call", {"cmd": "state"}, lambda: 1 / 0), {"day": 1})

    def test_a_request_may_not_set_the_fingerprint_itself(self) -> None:
        script = evidence.Transcript(ctx("save"), "game", "year", fingerprint=FP)
        with self.assertRaises(evidence.EvidenceError):
            script.begin("w0", {"fingerprint": {"x": 1}})

    def test_saved_mode_connects_to_nothing_so_nothing_is_checked(self) -> None:
        script = evidence.Transcript(ctx("saved"), "game", "year", fingerprint=FP)
        script.check_source({"game": "9.9"})

    def test_fresh_refuses_a_source_that_differs(self) -> None:
        script = evidence.Transcript(ctx("fresh"), "game", "year", fingerprint=FP)
        with self.assertRaises(evidence.SourceMismatch) as caught:
            script.check_source({"game": "0.72", "protocol": "p1"})
        message = str(caught.exception)
        self.assertIn('game: recorded against "0.71", the source reports "0.72"', message)
        self.assertNotIn("protocol", message)
        self.assertIn("re-record with save", message)

    def test_save_refuses_too_and_a_missing_report_is_named(self) -> None:
        script = evidence.Transcript(ctx("save"), "game", "year", fingerprint=FP)
        with self.assertRaises(evidence.SourceMismatch) as caught:
            script.check_source({"protocol": "p1"})
        self.assertIn("game: recorded against \"0.71\", the source reports nothing", str(caught.exception))

    def test_a_matching_source_may_report_more(self) -> None:
        script = evidence.Transcript(ctx("fresh"), "game", "year", fingerprint=FP)
        script.check_source({"game": "0.71", "protocol": "p1", "heapMb": 12})

    def test_through_merges_a_fingerprint_into_the_request(self) -> None:
        evidence.through(ctx("save"), "feed", "c", {"q": 1}, lambda: {"a": 1}, fingerprint={"api": "v2"})
        saved = json.loads((evidence.DATA_DIR / "feed/c.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"], {"fingerprint": {"api": "v2"}, "q": 1})
        with self.assertRaises(evidence.EvidenceError) as caught:
            evidence.through(ctx("saved"), "feed", "c", {"q": 1}, dict, fingerprint={"api": "v3"})
        self.assertIn('fingerprint.api: saved "v2", requested "v3"', str(caught.exception))

    def test_through_without_a_fingerprint_is_unchanged(self) -> None:
        evidence.through(ctx("save"), "feed", "plain", {"q": 1}, lambda: {"a": 1})
        saved = json.loads((evidence.DATA_DIR / "feed/plain.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"], {"q": 1})


if __name__ == "__main__":
    unittest.main()
