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


if __name__ == "__main__":
    unittest.main()
