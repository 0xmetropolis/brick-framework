# BOILERPLATE: evidence@2
"""Small, Git-stable saved-example storage for this brick.

Copy this file with the brick boilerplate. It deliberately has no dependency
outside the Python standard library and no knowledge of the runner.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from ..contract import CONTRACT_VERSION


DATA_DIR = Path(__file__).with_name("data")
DEFAULT_MAX_BYTES = 122_880
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_REDACTED_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "cookie",
    "cookies",
    "headers",
    "password",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}
_MISSING = object()


class EvidenceError(RuntimeError):
    """Saved evidence is absent, unsafe, invalid, or does not match."""


def load_example(adapter: str, case: str, request: dict[str, Any]) -> dict[str, Any]:
    """Load a saved example and require its request to match exactly."""
    path = _example_path(adapter, case)
    if not path.is_file():
        known = sorted(item.stem for item in path.parent.glob("*.json")) if path.parent.is_dir() else []
        raise EvidenceError(
            f"saved example does not exist: {path} (cases saved for {adapter}: {', '.join(known) or 'none'}); "
            "record it once with run(..., save=True)"
        )

    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("contract_version") != CONTRACT_VERSION:
        raise EvidenceError(
            f"saved example contract version {record.get('contract_version')} does not match "
            f"{CONTRACT_VERSION}: {path}; re-record it with run(..., save=True)"
        )
    normalized_request = _redact(_normalize(request))
    if record.get("request") != normalized_request:
        differences = request_diff(record.get("request"), normalized_request)
        raise EvidenceError(
            f"saved request does not match case {adapter}/{case}, captured by run "
            f"{record.get('capture_run_id')}: {'; '.join(differences)}. If a version or protocol field "
            "changed, the example is stale: re-record it with run(..., save=True)"
        )
    if ("response" in record) == ("error" in record):
        raise EvidenceError(f"saved example must contain response or error: {path}")
    return record


def save_example(
    adapter: str,
    case: str,
    capture_run_id: str,
    request: dict[str, Any],
    *,
    response: Any = _MISSING,
    error: Any = _MISSING,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Atomically replace one canonical, size-limited saved example."""
    if (response is _MISSING) == (error is _MISSING):
        raise EvidenceError("provide exactly one of response or error")

    record: dict[str, Any] = {
        "adapter": adapter,
        "capture_run_id": capture_run_id,
        "case": case,
        "contract_version": CONTRACT_VERSION,
        "request": _redact(_normalize(request)),
        "schema_version": 1,
    }
    if response is not _MISSING:
        record["response"] = _redact(_normalize(response))
    else:
        record["error"] = _redact(_normalize(error))

    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise EvidenceError(
            f"saved evidence is {len(payload)} bytes; limit is {max_bytes} bytes. Store a projection "
            "of the response (the fields src uses), split a long session into cases per window, or "
            "raise limits.max_evidence_bytes in config.yml and say why in the same change"
        )

    path = _example_path(adapter, case)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".evidence-", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return path


def request_diff(saved: Any, requested: Any, limit: int = 5) -> list[str]:
    """Where two normalized requests first differ, as ``path: saved X, requested Y`` lines."""
    found: list[str] = []

    def walk(left: Any, right: Any, path: str) -> None:
        if len(found) >= limit:
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                walk(left.get(key, _MISSING), right.get(key, _MISSING), f"{path}.{key}" if path else key)
        elif isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
            for index, (a, b) in enumerate(zip(left, right)):
                walk(a, b, f"{path}[{index}]")
        elif left != right:
            show = lambda value: "absent" if value is _MISSING else json.dumps(value)[:80]
            found.append(f"{path or 'request'}: saved {show(left)}, requested {show(right)}")

    walk(saved, requested, "")
    return found or ["requests differ"]


def digest(value: Any) -> str:
    """A short stable digest of a JSON value, for requests that carry large state."""
    canonical = json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def provenance(record: dict[str, Any] | None = None) -> dict[str, Any]:
    """What an adapter returns beside a response so src can say whether it was recorded.

    Pass the saved example for a replay; pass nothing for a live answer.
    """
    if record is None:
        return {"recorded": False, "capture_run_id": None}
    return {"recorded": True, "capture_run_id": record["capture_run_id"]}


def _max_bytes(run_context: dict[str, Any]) -> int:
    return int(run_context.get("config", {}).get("limits", {}).get("max_evidence_bytes", DEFAULT_MAX_BYTES))


def through(
    run_context: dict[str, Any], adapter: str, case: str, request: dict[str, Any], live: Any
) -> tuple[Any, dict[str, Any]]:
    """The saved / fresh / save contract for one adapter call: returns ``(response, provenance)``.

    saved  replay the example for ``case`` (request must match; a recorded error is raised)
    fresh  call ``live()``; nothing is written
    save   call ``live()`` and atomically replace the example, a failure included
    ``live`` takes no arguments and returns a JSON value.
    """
    mode = run_context["mode"]
    if mode == "saved":
        record = load_example(adapter, case, request)
        if "error" in record:
            raise RuntimeError(f"saved adapter error: {record['error']}")
        return record["response"], provenance(record)
    try:
        response = live()
    except Exception as exc:
        if mode == "save":
            save_example(adapter, case, run_context["run_id"], request, error={"type": type(exc).__name__}, max_bytes=_max_bytes(run_context))
        raise
    if mode == "save":
        save_example(adapter, case, run_context["run_id"], request, response=response, max_bytes=_max_bytes(run_context))
    return response, provenance()


class Transcript:
    """A chatty source as windows: one saved example per window, operations replayed in order.

    A window is one example named ``<case>-<window>``. Its request (the window name plus what the
    caller puts in ``request``, for instance a protocol version and a digest of the starting state)
    must match, like any example, so one stale window is found and re-recorded alone. Its response is
    the ordered list of operations; in saved mode each ``op`` must equal the next recorded one.
    ``record`` is True (every window), a list of window names, or False; it only matters in save mode.
    """

    def __init__(self, run_context: dict[str, Any], adapter: str, case: str, record: Any = True) -> None:
        self.context, self.adapter, self.case, self.record = run_context, adapter, case, record
        self.mode = run_context["mode"]
        self.window = ""
        self.request: dict[str, Any] = {}
        self.entries: list[dict[str, Any]] = []
        self.cursor = 0

    def _wants(self, window: str) -> bool:
        return self.mode == "save" and (self.record is True or (isinstance(self.record, list) and window in self.record))

    def begin(self, window: str, request: dict[str, Any]) -> dict[str, Any]:
        """Close the open window and start the next; returns the provenance of the window."""
        self.end()
        self.window = window
        self.request = _normalize({"window": window, **request})
        self.entries, self.cursor = [], 0
        if self.mode == "saved":
            record = load_example(self.adapter, f"{self.case}-{window}", self.request)
            if "error" in record:
                raise RuntimeError(f"saved adapter error: {record['error']}")
            self.entries = record["response"]["transcript"]
            return provenance(record)
        return provenance()

    def op(self, kind: str, request: Any, live: Any) -> Any:
        """One operation: replayed in saved mode, otherwise ``live()`` and, when recording, kept."""
        request = _normalize(request)
        where = f"{self.case}-{self.window}"
        if self.mode == "saved":
            if self.cursor >= len(self.entries):
                raise EvidenceError(
                    f"saved window {where} ended after {self.cursor} operations; a further {kind} was requested; "
                    "re-record the window with save"
                )
            entry = self.entries[self.cursor]
            self.cursor += 1
            if entry["op"] != kind:
                raise EvidenceError(
                    f"saved window {where} mismatch at operation {self.cursor}: recorded a {entry['op']}, requested a {kind}; "
                    "re-record the window with save"
                )
            if entry["request"] != request:
                differences = request_diff(entry["request"], request)
                raise EvidenceError(
                    f"saved window {where} mismatch at operation {self.cursor} ({kind}): {'; '.join(differences)}; "
                    "re-record the window with save"
                )
            return entry["response"]
        response = _normalize(live())
        if self._wants(self.window):
            self.entries.append({"op": kind, "request": request, "response": response})
        return response

    def end(self) -> None:
        """Close the open window; in save mode a recorded window is written."""
        if self.window and self._wants(self.window):
            save_example(
                self.adapter, f"{self.case}-{self.window}", self.context["run_id"], self.request,
                response={"transcript": self.entries}, max_bytes=_max_bytes(self.context),
            )
        self.window = ""


def _example_path(adapter: str, case: str) -> Path:
    for label, value in (("adapter", adapter), ("case", case)):
        if not _SAFE_NAME.fullmatch(value):
            raise EvidenceError(f"unsafe {label} name: {value!r}")
    return DATA_DIR / adapter / f"{case}.json"


def _normalize(value: Any) -> Any:
    """Return the JSON representation or raise a useful evidence error."""
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise EvidenceError("evidence must be JSON-serializable") from exc


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in _REDACTED_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
