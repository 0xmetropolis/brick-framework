# BOILERPLATE: runner.run@7
"""The brick's repository-internal entry point.

A working runner, copied with the brick like ``input/tape.py``: it creates
the run ID, reads ``input/config.yml``, turns ``fresh`` and ``record`` into the
run's plan, builds the plain run context, calls ``src``, closes whatever the run
left open, writes the run's ledger entry and prunes old entries. Only the imports from the
brick tree change per brick (``execute`` and the contract), which is why copies
of it stay identical.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from ..contract import BrickInput, BrickOutput, PORTS
from ..src.logic import execute
from .rng import create_run_id

BRICK = Path(__file__).resolve().parents[1]
LEDGER = BRICK / "runner" / "ledger"
CONFIG = BRICK / "input" / "config.yml"
TEST_ENV = "BRICK_TEST"
LOCAL_SUFFIX = ".local.json"


def load_config(path: Path = CONFIG) -> dict[str, Any]:
    """Read ``config.yml``: nested ``key: value`` mappings, two-space indent, no lists.

    Values are integers, ``true``/``false``/``null``, quoted or bare strings.
    ``#`` starts a comment. Anything richer belongs in code, not in this file.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, separator, value = line.strip().partition(":")
        if not separator or not key:
            raise ValueError(f"{path}:{number}: expected 'key: value'")
        while stack[-1][0] >= indent:
            stack.pop()
        value = value.strip()
        if value == "":
            child: dict[str, Any] = {}
            stack[-1][1][key] = child
            stack.append((indent, child))
        else:
            stack[-1][1][key] = _scalar(value)
    return root


def _scalar(text: str) -> Any:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return {"true": True, "false": False, "null": None}.get(text, text)


def run(inputs: BrickInput, *, fresh: Any = False, record: Any = False) -> BrickOutput:
    """Run the brick and return its output.

    ``fresh`` and ``record`` each take False (nothing), True (every source port of
    this brick), or a collection of port names: one of this brick's sources
    (``"rates"``), a source reached through a followed joint (``"assistant.xai"``),
    or ``"*"`` for all of them. ``fresh`` performs those calls against the live
    source without changing tracked takes; ``record`` implies fresh for the same
    names and replaces the reviewed takes. Everything not named replays. Neither
    option reaches a sibling brick except through a joint's declared ``follow``.
    The run's ledger entry is tracked only when the run touched a source fresh,
    recorded, or failed; every other run (and every run with ``BRICK_TEST=1``,
    which smoke tests set) writes a local, gitignored entry.
    """
    plan = make_plan(fresh, record)
    run_id = create_run_id()
    config = load_config()
    entry: dict[str, Any] = {
        "run_id": run_id,
        "fresh": sorted(name for name in plan["fresh"] if "." not in name),
        "record": sorted(name for name in plan["record"] if "." not in name),
        "followed": sorted(name for name in plan["fresh"] if "." in name),
        "input": inputs,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    context: dict[str, Any] = {"run_id": run_id, "plan": plan, "config": config, "closers": []}
    failed = True
    error: Exception | None = None
    try:
        output = execute(inputs, context)
        failed = False
    except Exception as exc:
        error = exc
        entry["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _note_unrecorded(entry, context)
        _write_entry(entry, config)
        raise
    finally:
        problems = _close(context, failed, error)
    if problems:
        entry["error"] = {"type": type(problems[0]).__name__, "message": str(problems[0])}
        _note_unrecorded(entry, context)
        _write_entry(entry, config)
        raise problems[0]
    entry["output"] = output
    _note_unrecorded(entry, context)
    _write_entry(entry, config)
    return output


def _note_unrecorded(entry: dict[str, Any], context: dict[str, Any]) -> None:
    """List the sessions that opted out of a recording their port was asked for, so the entry does not overstate it."""
    names = sorted(set(context.get("unrecorded", ())))
    if names:
        entry["unrecorded"] = names


def make_plan(fresh: Any, record: Any) -> dict[str, tuple[str, ...]]:
    """Turn the ``fresh`` and ``record`` options into the set of port names each applies to.

    Names are this brick's own source ports, or ``<joint>.<source>`` for a source
    a joint follows. An unknown name is an error; ``record`` implies ``fresh``.
    """
    own = {name for name, port in PORTS.items() if port.get("kind") == "source"}
    followed = {
        f"{name}.{source}"
        for name, port in PORTS.items()
        if port.get("kind") == "joint"
        for source in port.get("follow", ())
    }

    def names(option: Any, label: str) -> set[str]:
        if option is None or option is False:
            return set()
        if option is True:
            return set(own)
        if isinstance(option, str):
            option = [option]
        picked: set[str] = set()
        for name in option:
            if name == "*":
                picked |= own | followed
            elif name in own or name in followed:
                picked.add(name)
            else:
                known = ", ".join(sorted(own | followed)) or "none"
                raise ValueError(f"{label}= names {name!r}, which is not a source port of this brick or one it follows (known: {known})")
        return picked

    recorded = names(record, "record")
    went_fresh = names(fresh, "fresh") | recorded
    return {"fresh": tuple(sorted(went_fresh)), "record": tuple(sorted(recorded))}


def _close(context: dict[str, Any], failed: bool, error: Exception | None) -> list[Exception]:
    """Close what the run left open, newest first, telling each closer whether the run failed and with what.

    A failing close never hides another, nor the run's own error.
    """
    problems: list[Exception] = []
    for closer in reversed(context.get("closers", [])):
        try:
            closer(failed, error)
        except Exception as exc:  # noqa: BLE001
            problems.append(exc)
    return problems


def _write_entry(entry: dict[str, Any], config: dict[str, Any]) -> None:
    """Write the run's ledger entry: tracked if it touched a source fresh or failed, else local."""
    limit = int(config.get("limits", {}).get("max_entry_bytes", 122_880))
    retained = int(config.get("retention", {}).get("entries", 10))
    touched = bool(entry["fresh"] or entry["followed"] or "error" in entry)
    tracked = touched and os.environ.get(TEST_ENV) != "1"
    suffix = ".json" if tracked else LOCAL_SUFFIX
    payload = _bounded(entry, limit)
    LEDGER.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".entry-", dir=LEDGER)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, LEDGER / f"{entry['run_id']}{suffix}")
    finally:
        Path(temporary).unlink(missing_ok=True)
    mine = sorted(
        path for path in LEDGER.glob("*.json") if path.name.endswith(LOCAL_SUFFIX) == (suffix == LOCAL_SUFFIX)
    )
    for old in mine[:-retained]:
        # Another thread's run may have pruned the same entry already.
        old.unlink(missing_ok=True)


def _plain(value: Any) -> Any:
    """JSON-safe form of a value. Bytes become their size and digest, never their repr."""
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()[:16]}
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)[:200]


def _summarize(value: Any, threshold: int) -> Any:
    """Shrink long strings and long lists in place of dropping the whole value."""
    if isinstance(value, str) and len(value) > threshold:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return {"chars": len(value), "sha256": digest, "head": value[:60]}
    if isinstance(value, dict):
        return {key: _summarize(item, threshold) for key, item in value.items()}
    if isinstance(value, list):
        kept = [_summarize(item, threshold) for item in value[:20]]
        return kept + [{"omitted_items": len(value) - 20}] if len(value) > 20 and threshold <= 500 else [
            _summarize(item, threshold) for item in value
        ]
    return value


def _bounded(entry: dict[str, Any], limit: int) -> bytes:
    """Serialize canonically within ``limit``, summarizing instead of dropping.

    Long strings and lists are replaced by their size, a digest and a head, so
    the entry still shows the shape of what went in and came out. Only if that
    is not enough is a whole field reduced to its top-level keys and size. Fields
    that were changed are listed under ``summarized``.
    """

    def encode(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")

    entry = _plain(entry)
    payload = encode(entry)
    changed: list[str] = []
    for threshold in (2000, 500, 120, 40):
        if len(payload) <= limit:
            return payload
        for field in ("output", "input"):
            if field in entry:
                shrunk = _summarize(entry[field], threshold)
                if shrunk != entry[field]:
                    entry = {**entry, field: shrunk}
                    if field not in changed:
                        changed.append(field)
        payload = encode({**entry, "summarized": changed})
    for field in ("output", "input"):
        if len(payload) <= limit:
            break
        if field in entry:
            value = entry[field]
            entry = {**entry, field: {"omitted_bytes": len(json.dumps(value)), "keys": sorted(value)[:20] if isinstance(value, dict) else None}}
            if field not in changed:
                changed.append(field)
            payload = encode({**entry, "summarized": changed})
    return payload
