"""Tests for the boilerplate bricks/example_brick/input/tape.py that every brick copies."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from bricks.example_brick.input import tape

LABEL = {"game": "0.71", "protocol": "p1"}
PORTS = {
    "feed": {"kind": "source", "epoch": 1},
    "game": {"kind": "source", "epoch": 3, "label": LABEL, "followable": True},
    "model": {"kind": "joint", "policy": "orchestrated", "pin": 1, "follow": ("xai",)},
}


def ctx(fresh: tuple = (), record: tuple = (), run_id: str = "run-2", max_bytes: int | None = None) -> dict:
    config = {"limits": {"max_take_bytes": max_bytes}} if max_bytes else {}
    return {
        "run_id": run_id,
        "config": config,
        "plan": {"fresh": tuple(sorted({*fresh, *record})), "record": tuple(sorted(record))},
    }


class TapeCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            mock.patch.object(tape, "TAPE_DIR", Path(tmp.name)),
            mock.patch.object(tape, "PORTS", PORTS),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return {"answer": self.calls}


class TakeTests(TapeCase):
    def record(self, take: str = "day1", request: dict | None = None, **kwargs):
        return tape.write("feed", take, "run-1", request or {"day": 1, "mod": "v1"}, response=kwargs.get("response", {"ok": True}))

    def test_round_trip_and_stamp(self) -> None:
        self.record()
        record = tape.load("feed", "day1", {"mod": "v1", "day": 1})
        self.assertEqual(record["response"], {"ok": True})
        self.assertEqual((record["source"], record["take"], record["epoch"], record["schema_version"]), ("feed", "day1", 1, 2))
        self.assertEqual(tape.stamp(record), {"recorded": True, "recorded_in": "run-1"})

    def test_stamp_without_a_take_is_live(self) -> None:
        self.assertEqual(tape.stamp(), {"recorded": False, "recorded_in": None})

    def test_a_changed_request_says_which_field_changed(self) -> None:
        self.record()
        with self.assertRaises(tape.TapeError) as caught:
            tape.load("feed", "day1", {"day": 1, "mod": "v2"})
        message = str(caught.exception)
        self.assertIn('mod: recorded "v1", requested "v2"', message)
        self.assertIn("recorded in run run-1", message)
        self.assertIn("re-record it with run(..., record={'feed'})", message)

    def test_a_take_at_another_epoch_is_stale(self) -> None:
        self.record()
        with mock.patch.object(tape, "PORTS", {**PORTS, "feed": {"kind": "source", "epoch": 2}}):
            with self.assertRaises(tape.TapeError) as caught:
                tape.load("feed", "day1", {"day": 1, "mod": "v1"})
        self.assertIn("take recorded at epoch 1, port 'feed' is at epoch 2", str(caught.exception))

    def test_the_brick_version_is_not_part_of_a_take(self) -> None:
        path = self.record()
        self.assertNotIn("contract_version", json.loads(path.read_text(encoding="utf-8")))

    def test_the_old_take_format_says_what_the_new_one_is_and_where_to_read_more(self) -> None:
        path = self.record()
        path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        with self.assertRaises(tape.TapeError) as caught:
            tape.load("feed", "day1", {})
        message = str(caught.exception)
        self.assertIn("is not in schema 2 (it has schema 1)", message)
        self.assertIn("schema_version 2, source, take, epoch, recorded_in, request and exactly one of response or error", message)
        self.assertIn("see the README, 'Moving a project to this version'", message)
        self.assertNotIn("convert", message)

    def test_diff_covers_nested_lists_and_absent_fields(self) -> None:
        recorded = {"a": {"b": [1, 2]}, "gone": 1}
        requested = {"a": {"b": [1, 3]}, "new": 2}
        diff = tape.request_diff(recorded, requested)
        self.assertIn("a.b[1]: recorded 2, requested 3", diff)
        self.assertIn("gone: recorded 1, requested absent", diff)
        self.assertIn("new: recorded absent, requested 2", diff)

    def test_diff_is_bounded(self) -> None:
        recorded = {str(i): i for i in range(20)}
        requested = {str(i): i + 1 for i in range(20)}
        self.assertEqual(len(tape.request_diff(recorded, requested)), 5)

    def test_a_missing_take_lists_the_takes_that_exist(self) -> None:
        self.record("day1")
        self.record("day2")
        with self.assertRaises(tape.TapeError) as caught:
            tape.load("feed", "day9", {"day": 9})
        self.assertIn("takes recorded for feed: day1, day2", str(caught.exception))
        self.assertIn("record={'feed'}", str(caught.exception))

    def test_a_missing_tape_says_none(self) -> None:
        with self.assertRaises(tape.TapeError) as caught:
            tape.load("feed", "x", {})
        self.assertIn("takes recorded for feed: none", str(caught.exception))

    def test_a_name_that_is_not_a_source_port_is_refused(self) -> None:
        for name in ("nothing", "model"):
            with self.assertRaises(tape.TapeError) as caught:
                tape.load(name, "x", {})
            self.assertIn(f"no source port named {name!r}", str(caught.exception))

    def test_unsafe_names_are_refused(self) -> None:
        with self.assertRaises(tape.TapeError):
            tape.load("feed", "../x", {})

    def test_an_oversize_take_says_what_to_do(self) -> None:
        with self.assertRaises(tape.TapeError) as caught:
            tape.write("feed", "big", "run-1", {}, response={"x": "y" * 500}, max_bytes=100)
        message = str(caught.exception)
        for hint in ("projection", "takes per window", "limits.max_take_bytes"):
            self.assertIn(hint, message)

    def test_digest_is_stable_and_order_free(self) -> None:
        self.assertEqual(tape.digest({"a": 1, "b": [2]}), tape.digest({"b": [2], "a": 1}))
        self.assertNotEqual(tape.digest({"a": 1}), tape.digest({"a": 2}))
        self.assertTrue(tape.digest({}).startswith("sha256:"))

    def test_the_file_stays_canonical_and_secrets_are_redacted(self) -> None:
        path = tape.write("feed", "c", "run-1", {"token": "abc", "q": 1}, response={"ok": 1})
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")
        self.assertEqual(json.loads(text)["request"], {"q": 1, "token": "<redacted>"})


class ModeTests(TapeCase):
    def test_mode_reads_the_plan_per_source(self) -> None:
        context = ctx(fresh=("feed",), record=("game",))
        self.assertEqual((tape.mode(context, "feed"), tape.mode(context, "game")), ("fresh", "record"))
        self.assertEqual(tape.mode(ctx(), "feed"), "replay")

    def test_follow_slices_the_plan_for_one_joint_and_limits_it_to_the_follow_list(self) -> None:
        context = ctx(fresh=("model.xai", "model.other", "feed"), record=("model.xai",))
        self.assertEqual(tape.follow(context, "model"), {"fresh": ["xai"], "record": ["xai"]})
        self.assertEqual(tape.follow(ctx(), "model"), {"fresh": [], "record": []})

    def test_follow_names_a_joint(self) -> None:
        with self.assertRaises(tape.TapeError):
            tape.follow(ctx(), "feed")


class OnceTests(TapeCase):
    def test_record_then_replay_round_trip(self) -> None:
        response, stamp = tape.once(ctx(record=("feed",)), "feed", "c", {"q": 1}, self.fetch)
        self.assertEqual((response, stamp), ({"answer": 1}, {"recorded": False, "recorded_in": None}))
        again, stamp = tape.once(ctx(), "feed", "c", {"q": 1}, self.fetch)
        self.assertEqual(again, {"answer": 1})
        self.assertEqual(stamp, {"recorded": True, "recorded_in": "run-2"})
        self.assertEqual(self.calls, 1)

    def test_fresh_calls_the_source_and_writes_nothing(self) -> None:
        tape.once(ctx(fresh=("feed",)), "feed", "c", {"q": 1}, self.fetch)
        with self.assertRaises(tape.TapeError):
            tape.once(ctx(), "feed", "c", {"q": 1}, self.fetch)

    def test_a_port_the_run_did_not_name_replays(self) -> None:
        tape.once(ctx(record=("feed",)), "feed", "c", {"q": 1}, self.fetch)
        before = self.calls
        tape.once(ctx(fresh=("game",)), "feed", "c", {"q": 1}, self.fetch)
        self.assertEqual(self.calls, before)

    def test_replay_never_calls_the_source(self) -> None:
        with self.assertRaises(tape.TapeError):
            tape.once(ctx(), "feed", "missing", {}, self.fetch)
        self.assertEqual(self.calls, 0)

    def test_a_failure_is_recorded_and_raised_when_replayed(self) -> None:
        def boom():
            raise ValueError("no")

        with self.assertRaises(ValueError):
            tape.once(ctx(record=("feed",)), "feed", "c", {}, boom)
        with self.assertRaises(RuntimeError) as caught:
            tape.once(ctx(), "feed", "c", {}, boom)
        self.assertIn("recorded adapter error: {'type': 'ValueError'}", str(caught.exception))

    def test_keep_errors_false_leaves_a_good_take_alone_on_failure(self) -> None:
        tape.once(ctx(record=("feed",)), "feed", "c", {"q": 1}, lambda: {"a": 1})

        def boom():
            raise ValueError("transient")

        with self.assertRaises(ValueError):
            tape.once(ctx(record=("feed",)), "feed", "c", {"q": 1}, boom, keep_errors=False)
        again, _ = tape.once(ctx(), "feed", "c", {"q": 1}, dict)
        self.assertEqual(again, {"a": 1})

    def test_the_default_still_records_a_failure(self) -> None:
        def boom():
            raise ValueError("x")

        with self.assertRaises(ValueError):
            tape.once(ctx(record=("feed",)), "feed", "c2", {"q": 1}, boom)
        with self.assertRaises(RuntimeError):
            tape.once(ctx(), "feed", "c2", {"q": 1}, dict)

    def test_the_size_limit_comes_from_the_run_context(self) -> None:
        with self.assertRaises(tape.TapeError):
            tape.once(ctx(record=("feed",), max_bytes=100), "feed", "c", {}, lambda: {"x": "y" * 500})

    def test_the_label_is_merged_into_the_request(self) -> None:
        tape.once(ctx(record=("game",)), "game", "c", {"q": 1}, lambda: {"a": 1})
        saved = json.loads((tape.TAPE_DIR / "game/c.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"], {"label": LABEL, "q": 1})
        self.assertEqual(saved["epoch"], 3)
        with mock.patch.object(tape, "PORTS", {**PORTS, "game": {**PORTS["game"], "label": {**LABEL, "game": "0.72"}}}):
            with self.assertRaises(tape.TapeError) as caught:
                tape.once(ctx(), "game", "c", {"q": 1}, dict)
        self.assertIn('label.game: recorded "0.71", requested "0.72"', str(caught.exception))

    def test_a_request_may_not_set_the_label_itself(self) -> None:
        with self.assertRaises(tape.TapeError):
            tape.once(ctx(record=("game",)), "game", "c", {"label": {"x": 1}}, dict)

    def test_a_port_with_no_label_leaves_the_request_unchanged(self) -> None:
        tape.once(ctx(record=("feed",)), "feed", "plain", {"q": 1}, lambda: {"a": 1})
        saved = json.loads((tape.TAPE_DIR / "feed/plain.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"], {"q": 1})


class SessionTests(TapeCase):
    def play(self, context: dict) -> list:
        log = []
        script = tape.session(context, "game", "year")
        script.take("w0", {"protocol": "p1", "after": "d0"})
        log.append(script.call("call", {"cmd": "state"}, lambda: {"day": 1}))
        log.append(script.call("advance", {"days": 1}, lambda: {"day": 2}))
        script.take("w1", {"protocol": "p1", "after": "d1"})
        log.append(script.call("call", {"cmd": "state"}, lambda: {"day": 2}))
        script.close()
        return log

    def test_windows_are_one_take_each_and_replay_in_order(self) -> None:
        self.play(ctx(record=("game",)))
        names = sorted(path.stem for path in tape.TAPE_DIR.glob("game/*.json"))
        self.assertEqual(names, ["year-w0", "year-w1"])
        script = tape.session(ctx(), "game", "year")
        stamp = script.take("w0", {"protocol": "p1", "after": "d0"})
        self.assertEqual(stamp["recorded"], True)
        self.assertEqual(script.call("call", {"cmd": "state"}, lambda: 1 / 0), {"day": 1})
        self.assertEqual(script.call("advance", {"days": 1}, lambda: 1 / 0), {"day": 2})

    def test_a_stale_window_is_found_alone_by_its_request(self) -> None:
        self.play(ctx(record=("game",)))
        script = tape.session(ctx(), "game", "year")
        with self.assertRaises(tape.TapeError) as caught:
            script.take("w1", {"protocol": "p2", "after": "d1"})
        self.assertIn('protocol: recorded "p1", requested "p2"', str(caught.exception))

    def test_a_changed_operation_names_the_field_and_the_operation(self) -> None:
        self.play(ctx(record=("game",)))
        script = tape.session(ctx(), "game", "year")
        script.take("w0", {"protocol": "p1", "after": "d0"})
        script.call("call", {"cmd": "state"}, lambda: 1 / 0)
        with self.assertRaises(tape.TapeError) as caught:
            script.call("advance", {"days": 2}, lambda: 1 / 0)
        message = str(caught.exception)
        self.assertIn("mismatch at operation 2 (advance)", message)
        self.assertIn("days: recorded 1, requested 2", message)
        self.assertIn("re-record the window", message)

    def test_a_different_kind_and_an_extra_operation_are_errors(self) -> None:
        self.play(ctx(record=("game",)))
        script = tape.session(ctx(), "game", "year")
        script.take("w1", {"protocol": "p1", "after": "d1"})
        with self.assertRaises(tape.TapeError) as kind:
            script.call("advance", {"days": 1}, lambda: 1 / 0)
        self.assertIn("recorded a call, requested a advance", str(kind.exception))
        script = tape.session(ctx(), "game", "year")
        script.take("w1", {"protocol": "p1", "after": "d1"})
        script.call("call", {"cmd": "state"}, lambda: 1 / 0)
        with self.assertRaises(tape.TapeError) as extra:
            script.call("call", {"cmd": "state"}, lambda: 1 / 0)
        self.assertIn("ended after 1 operations", str(extra.exception))

    def test_only_the_windows_a_run_visits_are_recorded(self) -> None:
        script = tape.session(ctx(record=("game",)), "game", "year")
        script.take("w1", {"protocol": "p1", "after": "d1"})
        script.call("call", {"cmd": "state"}, lambda: {"day": 2})
        script.close()
        self.assertEqual(sorted(path.stem for path in tape.TAPE_DIR.glob("game/*.json")), ["year-w1"])

    def test_fresh_records_nothing(self) -> None:
        script = tape.session(ctx(fresh=("game",)), "game", "year")
        script.take("w0", {})
        self.assertEqual(script.call("call", {}, lambda: {"ok": 1}), {"ok": 1})
        script.close()
        self.assertEqual(list(tape.TAPE_DIR.glob("game/*.json")), [])

    def test_the_label_is_in_every_window_request(self) -> None:
        self.play(ctx(record=("game",)))
        saved = json.loads((tape.TAPE_DIR / "game/year-w0.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["request"]["label"], LABEL)
        self.assertEqual(saved["request"]["window"], "w0")

    def test_going_fresh_or_recording_needs_a_label_on_the_port(self) -> None:
        with mock.patch.object(tape, "PORTS", {**PORTS, "game": {"kind": "source", "epoch": 1}}):
            for context in (ctx(fresh=("game",)), ctx(record=("game",))):
                with self.assertRaises(tape.TapeError) as caught:
                    tape.session(context, "game", "year")
                self.assertIn("needs a label in PORTS", str(caught.exception))
            tape.session(ctx(), "game", "year")

    def test_unlabelled_takes_replay_until_they_are_re_recorded(self) -> None:
        with mock.patch.object(tape, "PORTS", {**PORTS, "game": {"kind": "source", "epoch": 3}}):
            tape.write("game", "year-w0", "run-1", {"window": "w0"}, response={"transcript": [{"op": "call", "request": {}, "response": {"day": 1}}]})
            script = tape.session(ctx(), "game", "year")
            script.take("w0", {})
            self.assertEqual(script.call("call", {}, lambda: 1 / 0), {"day": 1})

    def test_the_runner_closer_writes_a_finished_window_and_drops_a_cut_short_one(self) -> None:
        for failed, expected in ((False, ["year-w0"]), (True, [])):
            context = ctx(record=("game",))
            script = tape.session(context, "game", "year")
            script.take("w0", {})
            script.call("call", {}, lambda: {"ok": 1})
            for closer in context["closers"]:
                closer(failed)
            names = sorted(path.stem for path in tape.TAPE_DIR.glob("game/*.json"))
            self.assertEqual(names, expected, failed)
            for path in tape.TAPE_DIR.glob("game/*.json"):
                path.unlink()

    def test_a_request_may_not_set_the_label_itself(self) -> None:
        script = tape.session(ctx(record=("game",)), "game", "year")
        with self.assertRaises(tape.TapeError):
            script.take("w0", {"label": {"x": 1}})


class SessionRecordOptOutTests(TapeCase):
    """``record=False`` can turn recording off for one session of a recording port; it can never turn it on."""

    def play(self, context: dict, name: str, **options) -> tape.Session:
        script = tape.session(context, "game", name, **options)
        script.take("w0", {})
        script.call("call", {}, lambda: {"n": name})
        script.close()
        return script

    def takes(self) -> list[str]:
        return sorted(path.stem for path in tape.TAPE_DIR.glob("game/*.json"))

    def test_one_session_records_and_another_of_the_same_port_stays_fresh_in_one_run(self) -> None:
        context = ctx(record=("game",))
        kept = self.play(context, "main")
        side = self.play(context, "side", record=False)
        self.assertEqual((kept.mode, side.mode), ("record", "fresh"))
        self.assertEqual(self.takes(), ["main-w0"])

    def test_the_opted_out_session_still_answers_live_and_leaves_an_existing_take_untouched(self) -> None:
        self.play(ctx(record=("game",)), "side")
        path = tape.TAPE_DIR / "game/side-w0.json"
        before = path.read_bytes()
        context = ctx(record=("game",), run_id="run-3")
        script = tape.session(context, "game", "side", record=False)
        script.take("w0", {})
        self.assertEqual(script.call("call", {}, lambda: {"n": "newer"}), {"n": "newer"})
        script.close()
        for closer in context["closers"]:
            closer(False)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(before)["epoch"], 3)

    def test_it_still_needs_the_label_because_it_still_goes_to_the_source(self) -> None:
        with mock.patch.object(tape, "PORTS", {**PORTS, "game": {"kind": "source", "epoch": 1}}):
            with self.assertRaises(tape.TapeError) as caught:
                tape.session(ctx(record=("game",)), "game", "side", record=False)
        self.assertIn("needs a label in PORTS", str(caught.exception))

    def test_verify_still_checks_the_source_for_an_opted_out_session(self) -> None:
        script = tape.session(ctx(record=("game",)), "game", "side", record=False)
        with self.assertRaises(tape.LabelMismatch):
            script.verify({"game": "9.9"})

    def test_a_downgrade_is_listed_for_the_ledger_and_nothing_else_is(self) -> None:
        context = ctx(record=("game",))
        tape.session(context, "game", "kept")
        tape.session(context, "game", "side", record=False)
        tape.session(context, "game", "again", record=True)
        self.assertEqual(context["unrecorded"], ["game.side"])

    def test_it_cannot_turn_recording_on_for_a_port_the_plan_does_not_record(self) -> None:
        for plan, expected in (({}, "replay"), ({"fresh": ("game",)}, "fresh")):
            for option in (True, False):
                with self.subTest(plan=plan, record=option):
                    context = ctx(**plan)
                    script = tape.session(context, "game", "x", record=option)
                    self.assertEqual(script.mode, expected)
                    self.assertNotIn("unrecorded", context)

    def test_record_true_is_accepted_and_means_as_the_plan_says(self) -> None:
        explicit = self.play(ctx(record=("game",)), "a", record=True)
        default = self.play(ctx(record=("game",)), "b")
        self.assertEqual((explicit.mode, default.mode), ("record", "record"))
        self.assertEqual(self.takes(), ["a-w0", "b-w0"])

    def test_replay_of_an_opted_out_session_is_ordinary_replay(self) -> None:
        self.play(ctx(record=("game",)), "main")
        script = tape.session(ctx(), "game", "main", record=False)
        script.take("w0", {})
        self.assertEqual(script.call("call", {}, lambda: 1 / 0), {"n": "main"})

    def test_record_must_be_a_bool(self) -> None:
        for bad in (None, 0, 1, "no"):
            with self.assertRaises(tape.TapeError) as caught:
                tape.session(ctx(record=("game",)), "game", "x", record=bad)
            self.assertIn("record must be True or False", str(caught.exception))


class FailedSessionTests(TapeCase):
    """A session whose fetch raised, and whose exception ended the run, is written as a failed take."""

    class Source(Exception):
        """What a live source raises."""

    def run_session(self, fetches: list, *, name: str = "year", plan=None, wrap=None, after=None) -> list:
        """Run one window the way a brick would, then let the runner's closers see the run end."""
        context = ctx(record=("game",)) if plan is None else plan
        outcome: Exception | None = None
        try:
            script = tape.session(context, "game", name)
            script.take("w0", {"after": "d0"})
            for index, fetch in enumerate(fetches):
                script.call("op", {"i": index}, fetch)
            if after:
                raise after
        except Exception as exc:  # noqa: BLE001
            outcome = wrap(exc) if wrap else exc
        for closer in reversed(context["closers"]):
            closer(outcome is not None, outcome)
        return [path.stem for path in sorted(tape.TAPE_DIR.glob("game/*.json"))]

    def boom(self):
        raise self.Source("the source went away")

    def transcript(self, take: str = "year-w0") -> list:
        return json.loads((tape.TAPE_DIR / f"game/{take}.json").read_text(encoding="utf-8"))["response"]["transcript"]

    def test_the_fetch_that_raised_ends_the_take_with_its_error(self) -> None:
        self.assertEqual(self.run_session([lambda: {"n": 1}, lambda: {"n": 2}, self.boom]), ["year-w0"])
        self.assertEqual(
            self.transcript(),
            [
                {"op": "op", "request": {"i": 0}, "response": {"n": 1}},
                {"op": "op", "request": {"i": 1}, "response": {"n": 2}},
                {"op": "op", "request": {"i": 2}, "error": {"type": "Source"}},
            ],
        )

    def test_replay_plays_the_recorded_operations_then_raises_the_recorded_error(self) -> None:
        self.run_session([lambda: {"n": 1}, self.boom])
        script = tape.session(ctx(), "game", "year")
        script.take("w0", {"after": "d0"})
        self.assertEqual(script.call("op", {"i": 0}, lambda: 1 / 0), {"n": 1})
        with self.assertRaises(RuntimeError) as caught:
            script.call("op", {"i": 1}, lambda: 1 / 0)
        self.assertEqual(str(caught.exception), "recorded adapter error: {'type': 'Source'}")

    def test_the_failed_take_is_a_valid_take_and_goes_stale_like_any_other(self) -> None:
        self.run_session([self.boom])
        record = json.loads((tape.TAPE_DIR / "game/year-w0.json").read_text(encoding="utf-8"))
        self.assertEqual((record["schema_version"], record["epoch"], record["source"], "response" in record), (2, 3, "game", True))
        self.assertEqual(record["request"]["label"], LABEL)
        with mock.patch.object(tape, "PORTS", {**PORTS, "game": {**PORTS["game"], "epoch": 4}}):
            script = tape.session(ctx(), "game", "year")
            with self.assertRaises(tape.TapeError) as caught:
                script.take("w0", {"after": "d0"})
        self.assertIn("take recorded at epoch 3, port 'game' is at epoch 4", str(caught.exception))

    def test_an_exception_the_brick_wrapped_still_counts_when_the_fetch_failure_is_its_cause(self) -> None:
        class Wrapped(Exception):
            pass

        def wrap(exc):
            try:
                raise Wrapped("brick says no") from exc
            except Wrapped as wrapped:
                return wrapped

        self.assertEqual(self.run_session([self.boom], wrap=wrap), ["year-w0"])

    def test_an_exception_raised_while_handling_the_fetch_failure_counts_through_its_context(self) -> None:
        def wrap(exc):
            try:
                try:
                    raise exc
                except Exception:
                    raise KeyError("while handling")
            except KeyError as inner:
                return inner

        self.assertEqual(self.run_session([self.boom], wrap=wrap), ["year-w0"])

    def test_nothing_is_written_for_a_failure_that_is_not_the_fetch(self) -> None:
        self.assertEqual(self.run_session([lambda: {"n": 1}], after=ValueError("a bug in the brick after the fetch")), [])

    def test_nothing_is_written_when_the_brick_caught_the_fetch_failure_and_then_failed_otherwise(self) -> None:
        context = ctx(record=("game",))
        script = tape.session(context, "game", "year")
        script.take("w0", {"after": "d0"})
        with self.assertRaises(self.Source):
            script.call("op", {"i": 0}, self.boom)
        script.call("op", {"i": 1}, lambda: {"n": 1})
        for closer in context["closers"]:
            closer(True, ValueError("something else"))
        self.assertEqual(list(tape.TAPE_DIR.glob("game/*.json")), [])

    def test_a_brick_that_catches_the_failure_and_finishes_writes_an_ordinary_take_without_the_failed_op(self) -> None:
        context = ctx(record=("game",))
        script = tape.session(context, "game", "year")
        script.take("w0", {"after": "d0"})
        try:
            script.call("op", {"i": 0}, self.boom)
        except self.Source:
            pass
        script.call("op", {"i": 1}, lambda: {"n": 1})
        for closer in context["closers"]:
            closer(False, None)
        self.assertEqual(self.transcript(), [{"op": "op", "request": {"i": 1}, "response": {"n": 1}}])

    def test_nothing_is_written_when_the_run_was_interrupted_or_only_failed_without_an_error(self) -> None:
        context = ctx(record=("game",))
        script = tape.session(context, "game", "year")
        script.take("w0", {"after": "d0"})
        with self.assertRaises(self.Source):
            script.call("op", {"i": 0}, self.boom)
        for closer in context["closers"]:
            closer(True, None)
        self.assertEqual(list(tape.TAPE_DIR.glob("game/*.json")), [])

    def test_fresh_and_replay_and_an_opted_out_session_never_write_a_failed_take(self) -> None:
        self.assertEqual(self.run_session([self.boom], plan=ctx(fresh=("game",))), [])
        context = ctx(record=("game",))
        out: Exception | None = None
        try:
            script = tape.session(context, "game", "side", record=False)
            script.take("w0", {})
            script.call("op", {}, self.boom)
        except self.Source as exc:
            out = exc
        for closer in context["closers"]:
            closer(True, out)
        self.assertEqual(list(tape.TAPE_DIR.glob("game/*.json")), [])

    def test_it_never_replaces_an_existing_complete_take(self) -> None:
        self.run_session([lambda: {"n": "good"}])
        path = tape.TAPE_DIR / "game/year-w0.json"
        before = path.read_bytes()
        # The same request, so the take is current; this time the source fails at the first operation.
        self.run_session([self.boom])
        self.assertEqual(path.read_bytes(), before)

    def test_it_does_replace_an_earlier_failed_take_and_a_missing_or_unreadable_one(self) -> None:
        self.run_session([lambda: {"n": 1}, self.boom])
        self.assertEqual(len(self.transcript()), 2)
        self.run_session([self.boom])
        self.assertEqual(len(self.transcript()), 1)
        (tape.TAPE_DIR / "game/year-w0.json").write_text("not json", encoding="utf-8")
        self.run_session([lambda: {"n": 1}, self.boom])
        self.assertEqual(len(self.transcript()), 2)

    def test_earlier_windows_of_the_session_are_already_complete_takes(self) -> None:
        context = ctx(record=("game",))
        script = tape.session(context, "game", "year")
        script.take("w0", {})
        script.call("op", {}, lambda: {"n": 1})
        script.take("w1", {})
        out = None
        try:
            script.call("op", {}, self.boom)
        except self.Source as exc:
            out = exc
        for closer in context["closers"]:
            closer(True, out)
        self.assertEqual(sorted(p.stem for p in tape.TAPE_DIR.glob("game/*.json")), ["year-w0", "year-w1"])
        self.assertNotIn("error", json.dumps(self.transcript("year-w0")))
        self.assertEqual(self.transcript("year-w1"), [{"op": "op", "request": {}, "error": {"type": "Source"}}])

    def test_a_failed_take_too_big_to_write_raises_from_the_closer_and_the_runner_keeps_the_run_error(self) -> None:
        context = ctx(record=("game",), max_bytes=200)
        script = tape.session(context, "game", "year")
        script.take("w0", {})
        script.call("op", {}, lambda: {"pad": "x" * 10})
        out = None
        try:
            script.call("op", {}, self.boom)
        except self.Source as exc:
            out = exc
        with self.assertRaises(tape.TapeError):
            for closer in context["closers"]:
                closer(True, out)

    def test_the_old_one_argument_closer_call_still_means_a_clean_close_or_a_drop(self) -> None:
        context = ctx(record=("game",))
        script = tape.session(context, "game", "year")
        script.take("w0", {})
        script.call("op", {}, lambda: {"n": 1})
        context["closers"][0](False)
        self.assertEqual(len(self.transcript()), 1)


class VerifyTests(TapeCase):
    def test_replay_connects_to_nothing_so_nothing_is_checked(self) -> None:
        tape.verify(ctx(), "game", {"game": "9.9"})
        tape.session(ctx(), "game", "year").verify({"game": "9.9"})

    def test_fresh_refuses_a_source_that_differs(self) -> None:
        script = tape.session(ctx(fresh=("game",)), "game", "year")
        with self.assertRaises(tape.LabelMismatch) as caught:
            script.verify({"game": "0.72", "protocol": "p1"})
        message = str(caught.exception)
        self.assertIn('game: recorded against "0.71", the source reports "0.72"', message)
        self.assertNotIn("protocol", message)
        self.assertIn("change the label in PORTS and re-record", message)
        self.assertIsInstance(caught.exception, tape.TapeError)

    def test_record_refuses_too_and_a_missing_report_is_named(self) -> None:
        with self.assertRaises(tape.LabelMismatch) as caught:
            tape.verify(ctx(record=("game",)), "game", {"protocol": "p1"})
        self.assertIn('game: recorded against "0.71", the source reports nothing', str(caught.exception))

    def test_a_matching_source_may_report_more(self) -> None:
        tape.verify(ctx(fresh=("game",)), "game", {"game": "0.71", "protocol": "p1", "heapMb": 12})

    def test_a_stale_label_is_a_stale_take_error_not_a_source_mismatch(self) -> None:
        tape.once(ctx(record=("game",)), "game", "c", {}, lambda: {"a": 1})
        with mock.patch.object(tape, "PORTS", {**PORTS, "game": {**PORTS["game"], "label": {"game": "0.9", "protocol": "p1"}}}):
            with self.assertRaises(tape.TapeError) as caught:
                tape.once(ctx(), "game", "c", {}, dict)
        self.assertNotIsInstance(caught.exception, tape.LabelMismatch)


if __name__ == "__main__":
    unittest.main()
