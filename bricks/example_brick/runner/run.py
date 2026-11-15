# BOILERPLATE: runner.run@4
"""The brick's repository-internal entry point.

A working runner, copied with the brick like ``input/evidence.py``: it creates
the run ID, reads ``input/config.yml``, builds the plain run context, calls
``src``, records the run and prunes old records. Only the two imports from the
brick tree change per brick (``execute`` and the contract types), which is why
copies of it stay identical.
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

from ..contract import BrickInput, BrickOutput
from ..src.logic import execute
from .rng import create_run_id

BRICK = Path(__file__).resolve().parents[1]
RUNS = BRICK / "runner" / "runs"
CONFIG = BRICK / "input" / "config.yml"
KIND_ENV = "BRICK_RUN_KIND"
TEST_SUFFIX = ".test.json"


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


def run(inputs: BrickInput, *, fresh: bool = False, save: bool = False) -> BrickOutput:
    """Run the brick and return its output.

    ``fresh`` performs this brick's external calls without changing tracked
    examples. ``save`` implies fresh and replaces reviewed tracked examples.
    Neither option is forwarded to sibling bricks, except by mode passing (a workflow's
    ``pass_mode`` to a declared Live Adapter sibling). Siblings that ran fresh are listed
    in the record as ``fresh_siblings``. Set ``BRICK_RUN_KIND=test``
    (smoke tests do) and the record is a local ``*.test.json`` instead of
    tracked history.
    """
    run_id = create_run_id()
    config = load_config()
    mode = "save" if save else "fresh" if fresh else "saved"
    record: dict[str, Any] = {
        "run_id": run_id,
        "kind": "test" if os.environ.get(KIND_ENV) == "test" else "run",
        "mode": mode,
        "input": inputs,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    context: dict[str, Any] = {"run_id": run_id, "mode": mode, "config": config}
    try:
        output = execute(inputs, context)
    except Exception as exc:
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _note_fresh_siblings(record, context)
        _record(record, config)
        raise
    _note_fresh_siblings(record, context)
    record["output"] = output
    _record(record, config)
    return output


def _note_fresh_siblings(record: dict[str, Any], context: dict[str, Any]) -> None:
    """Copy the siblings that ran fresh into the record. Record-keeping only: nothing reads it back."""
    names = context.get("fresh_siblings")
    if names:
        record["fresh_siblings"] = sorted({str(name) for name in names})


def _record(record: dict[str, Any], config: dict[str, Any]) -> None:
    limit = int(config.get("limits", {}).get("max_run_record_bytes", 122_880))
    retained = int(config.get("retention", {}).get("runs", 10))
    payload = _bounded(record, limit)
    suffix = TEST_SUFFIX if record["kind"] == "test" else ".json"
    RUNS.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".run-", dir=RUNS)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
        os.replace(temporary, RUNS / f"{record['run_id']}{suffix}")
    finally:
        Path(temporary).unlink(missing_ok=True)
    mine = sorted(
        path for path in RUNS.glob("*.json") if path.name.endswith(TEST_SUFFIX) == (suffix == TEST_SUFFIX)
    )
    for old in mine[:-retained]:
        # Another thread's run may have pruned the same record already.
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


def _bounded(record: dict[str, Any], limit: int) -> bytes:
    """Serialize canonically within ``limit``, summarizing instead of dropping.

    Long strings and lists are replaced by their size, a digest and a head, so
    the record still shows the shape of what went in and came out. Only if that
    is not enough is a whole field reduced to its top-level keys and size. Fields
    that were changed are listed under ``summarized``.
    """

    def encode(value: dict[str, Any]) -> bytes:
        return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")

    record = _plain(record)
    payload = encode(record)
    changed: list[str] = []
    for threshold in (2000, 500, 120, 40):
        if len(payload) <= limit:
            return payload
        for field in ("output", "input"):
            if field in record:
                shrunk = _summarize(record[field], threshold)
                if shrunk != record[field]:
                    record = {**record, field: shrunk}
                    if field not in changed:
                        changed.append(field)
        payload = encode({**record, "summarized": changed})
    for field in ("output", "input"):
        if len(payload) <= limit:
            break
        if field in record:
            value = record[field]
            record = {**record, field: {"omitted_bytes": len(json.dumps(value)), "keys": sorted(value)[:20] if isinstance(value, dict) else None}}
            if field not in changed:
                changed.append(field)
            payload = encode({**record, "summarized": changed})
    return payload
