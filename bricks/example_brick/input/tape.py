# BOILERPLATE: tape@1
"""Takes and the replay / fresh / record contract for this brick's source ports.

Copy this file with the brick boilerplate. It has no dependency outside the
Python standard library and this brick's own ``contract.py``, and no knowledge of
the runner: it reads the run's plan from the run context and each source port's
epoch and label from ``PORTS``.

A *tape* is everything recorded from one source port; a *take* is one recorded
file on it, ``tapes/<source>/<take>.json``. Replay is the default. ``fresh`` is
the one switch to the live source; ``record`` is ``fresh`` plus writing the take.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable

from ..contract import PORTS

TAPE_DIR = Path(__file__).with_name("tapes")
DEFAULT_MAX_BYTES = 122_880
SCHEMA_VERSION = 2
CONVERTER = "tools/convert_ports.py"
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


class TapeError(RuntimeError):
    """A take is absent, unsafe, invalid, stale, or does not match."""


class LabelMismatch(TapeError):
    """The live source is not the one the takes were recorded against."""


def _port(source: str) -> dict[str, Any]:
    port = PORTS.get(source)
    if not isinstance(port, dict) or port.get("kind") != "source":
        raise TapeError(f"no source port named {source!r} in PORTS (contract.py)")
    return port


def mode(run_context: dict[str, Any], source: str) -> str:
    """``replay``, ``fresh`` or ``record`` for one of this brick's source ports, from the run's plan."""
    plan = run_context["plan"]
    if source in plan["record"]:
        return "record"
    if source in plan["fresh"]:
        return "fresh"
    return "replay"


def follow(run_context: dict[str, Any], joint: str) -> dict[str, list[str]]:
    """The slice of this run's plan that reaches the sibling behind ``joint``.

    A joint adapter calls the sibling like this and nothing else is allowed to
    supply those two keywords (the linter checks)::

        flags = follow(run_context, "game")
        run(request, fresh=flags["fresh"], record=flags["record"])

    Each list holds the sibling's own source names, without the ``<joint>.``
    prefix, limited to the sources the joint declares in ``follow``.
    """
    port = PORTS.get(joint)
    if not isinstance(port, dict) or port.get("kind") != "joint":
        raise TapeError(f"no joint port named {joint!r} in PORTS (contract.py)")
    allowed = set(port.get("follow", ()))
    prefix = joint + "."
    plan = run_context["plan"]

    def pick(names: Any) -> list[str]:
        return sorted(name[len(prefix):] for name in names if name.startswith(prefix) and name[len(prefix):] in allowed)

    return {"fresh": pick(plan["fresh"]), "record": pick(plan["record"])}


def stamp(record: dict[str, Any] | None = None) -> dict[str, Any]:
    """What an adapter returns beside a response so ``src`` can say whether it was recorded.

    Pass the take for a replay; pass nothing for a live answer.
    """
    if record is None:
        return {"recorded": False, "recorded_in": None}
    return {"recorded": True, "recorded_in": record["recorded_in"]}


def verify(run_context: dict[str, Any], source: str, seen: dict[str, Any]) -> None:
    """On connect, refuse to continue when the live source differs from the port's label.

    The label is what the takes were recorded against (a game version, a build, a
    protocol); ``seen`` is what the live source reports now. Only the keys of the
    label are compared, so the source may report more. Replay connects to nothing
    and passes. A mismatch is an error in fresh and record: a run against a
    different source would record or compare the wrong thing.
    """
    if mode(run_context, source) == "replay":
        return
    label = _port(source).get("label") or {}
    differences = [
        f"{key}: recorded against {json.dumps(expected)}, the source reports "
        + (json.dumps(seen[key]) if key in seen else "nothing")
        for key, expected in sorted(label.items())
        if seen.get(key) != expected
    ]
    if differences:
        raise LabelMismatch(
            f"the live source does not match the label of the takes for {source!r} ("
            + "; ".join(differences)
            + "); use the matching source, or change the label in PORTS and re-record"
        )


def _with_label(source: str, request: dict[str, Any]) -> dict[str, Any]:
    label = _port(source).get("label")
    if not label:
        return request
    if "label" in request:
        raise TapeError("the request already has a 'label' key; the port's label is added for you")
    return {"label": label, **request}


def load(source: str, take: str, request: dict[str, Any]) -> dict[str, Any]:
    """Load a take; its epoch must be the port's and its request must match exactly."""
    port = _port(source)
    path = _take_path(source, take)
    if not path.is_file():
        known = sorted(item.stem for item in path.parent.glob("*.json")) if path.parent.is_dir() else []
        raise TapeError(
            f"take does not exist: {path} (takes recorded for {source}: {', '.join(known) or 'none'}); "
            f"record it once with run(..., record={{{source!r}}})"
        )

    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("schema_version") != SCHEMA_VERSION:
        raise TapeError(
            f"take {path} is in the old format (schema {record.get('schema_version')}); "
            f"run python3 {CONVERTER} once to convert the project"
        )
    epoch = port["epoch"]
    if record.get("epoch") != epoch:
        raise TapeError(
            f"take recorded at epoch {record.get('epoch')}, port {source!r} is at epoch {epoch}: {path}; "
            f"re-record it with run(..., record={{{source!r}}})"
        )
    normalized_request = _redact(_normalize(_with_label(source, request)))
    if record.get("request") != normalized_request:
        differences = request_diff(record.get("request"), normalized_request)
        raise TapeError(
            f"recorded request does not match take {source}/{take}, recorded in run "
            f"{record.get('recorded_in')}: {'; '.join(differences)}. If a label or version field "
            f"changed, the take is stale: re-record it with run(..., record={{{source!r}}})"
        )
    if ("response" in record) == ("error" in record):
        raise TapeError(f"a take must contain response or error: {path}")
    return record


def write(
    source: str,
    take: str,
    recorded_in: str,
    request: dict[str, Any],
    *,
    response: Any = _MISSING,
    error: Any = _MISSING,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> Path:
    """Atomically replace one canonical, size-limited take."""
    if (response is _MISSING) == (error is _MISSING):
        raise TapeError("provide exactly one of response or error")

    record: dict[str, Any] = {
        "epoch": _port(source)["epoch"],
        "recorded_in": recorded_in,
        "request": _redact(_normalize(_with_label(source, request))),
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "take": take,
    }
    if response is not _MISSING:
        record["response"] = _redact(_normalize(response))
    else:
        record["error"] = _redact(_normalize(error))

    payload = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > max_bytes:
        raise TapeError(
            f"take is {len(payload)} bytes; limit is {max_bytes} bytes. Store a projection "
            "of the response (the fields src uses), split a long session into takes per window, or "
            "raise limits.max_take_bytes in config.yml and say why in the same change"
        )

    path = _take_path(source, take)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".take-", dir=path.parent)
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


def request_diff(recorded: Any, requested: Any, limit: int = 5) -> list[str]:
    """Where two normalized requests first differ, as ``path: recorded X, requested Y`` lines."""
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
            found.append(f"{path or 'request'}: recorded {show(left)}, requested {show(right)}")

    walk(recorded, requested, "")
    return found or ["requests differ"]


def digest(value: Any) -> str:
    """A short stable digest of a JSON value, for requests that carry large state."""
    canonical = json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _max_bytes(run_context: dict[str, Any]) -> int:
    return int(run_context.get("config", {}).get("limits", {}).get("max_take_bytes", DEFAULT_MAX_BYTES))


def once(
    run_context: dict[str, Any], source: str, take: str, request: dict[str, Any], fetch: Callable[[], Any],
    *, keep_errors: bool = True,
) -> tuple[Any, dict[str, Any]]:
    """The replay / fresh / record contract for one call on a source port: returns ``(response, stamp)``.

    replay  load the take (epoch and request must match; a recorded error is raised)
    fresh   call ``fetch()``; nothing is written
    record  call ``fetch()`` and atomically replace the take, a failure included
            (pass ``keep_errors=False`` for a paid or hard-to-repeat call: a failure then
            raises and leaves the take as it was)
    ``fetch`` takes no arguments and returns a JSON value. The port's label (a game
    version, a build, a protocol) is merged into the request, so a take recorded
    against another source version is stale and says which field changed.
    """
    how = mode(run_context, source)
    if how == "replay":
        record = load(source, take, request)
        if "error" in record:
            raise RuntimeError(f"recorded adapter error: {record['error']}")
        return record["response"], stamp(record)
    try:
        response = fetch()
    except Exception as exc:
        if how == "record" and keep_errors:
            write(source, take, run_context["run_id"], request, error={"type": type(exc).__name__}, max_bytes=_max_bytes(run_context))
        raise
    if how == "record":
        write(source, take, run_context["run_id"], request, response=response, max_bytes=_max_bytes(run_context))
    return response, stamp()


def session(run_context: dict[str, Any], source: str, name: str) -> "Session":
    """A chatty source as takes: one take per window, operations replayed in order.

    The runner closes any session still open when the run ends, so a failed run
    leaves no half-written window behind.
    """
    return Session(run_context, source, name)


class Session:
    """One take per window, named ``<name>-<window>``, with the operations replayed in order.

    A window's request (the window name plus what the caller puts in ``request``, for
    instance a protocol version and a digest of the starting state) must match like any
    take, so one stale window is found and re-recorded alone. Its response is the
    ordered list of operations; in replay each ``call`` must equal the next recorded one.

    Going fresh or recording needs a label on the port: what the source is (a game
    version, a build, a protocol). It is merged into every window's request, so a window
    recorded against another version is stale and the error names the field. After
    connecting in fresh or record, call ``verify(seen)``.
    """

    def __init__(self, run_context: dict[str, Any], source: str, name: str) -> None:
        port = _port(source)
        self.mode = mode(run_context, source)
        if self.mode != "replay" and not port.get("label"):
            raise TapeError(
                f"a session on source {source!r} needs a label in PORTS to go fresh or record: "
                "say what the source is (a game version, a build, a protocol)"
            )
        self.context, self.source, self.name = run_context, source, name
        self.window = ""
        self.request: dict[str, Any] = {}
        self.entries: list[dict[str, Any]] = []
        self.cursor = 0
        run_context.setdefault("closers", []).append(self._finish)

    def verify(self, seen: dict[str, Any]) -> None:
        """Compare what the live source reports with the port's label; refuse to continue on a mismatch."""
        verify(self.context, self.source, seen)

    def take(self, window: str, request: dict[str, Any]) -> dict[str, Any]:
        """Close the open window and start the next; returns the stamp of the window."""
        self.close()
        self.window = window
        self.request = _normalize({"window": window, **request})
        _with_label(self.source, self.request)
        self.entries, self.cursor = [], 0
        if self.mode == "replay":
            record = load(self.source, f"{self.name}-{window}", self.request)
            if "error" in record:
                raise RuntimeError(f"recorded adapter error: {record['error']}")
            self.entries = record["response"]["transcript"]
            return stamp(record)
        return stamp()

    def call(self, kind: str, request: Any, fetch: Callable[[], Any]) -> Any:
        """One operation: replayed, otherwise ``fetch()`` and, when recording, kept."""
        request = _normalize(request)
        where = f"{self.name}-{self.window}"
        if self.mode == "replay":
            if self.cursor >= len(self.entries):
                raise TapeError(
                    f"recorded window {where} ended after {self.cursor} operations; a further {kind} was requested; "
                    "re-record the window"
                )
            entry = self.entries[self.cursor]
            self.cursor += 1
            if entry["op"] != kind:
                raise TapeError(
                    f"recorded window {where} mismatch at operation {self.cursor}: recorded a {entry['op']}, requested a {kind}; "
                    "re-record the window"
                )
            if entry["request"] != request:
                differences = request_diff(entry["request"], request)
                raise TapeError(
                    f"recorded window {where} mismatch at operation {self.cursor} ({kind}): {'; '.join(differences)}; "
                    "re-record the window"
                )
            return entry["response"]
        response = _normalize(fetch())
        if self.mode == "record":
            self.entries.append({"op": kind, "request": request, "response": response})
        return response

    def close(self) -> None:
        """Close the open window; when recording, the window is written."""
        if self.window and self.mode == "record":
            write(
                self.source, f"{self.name}-{self.window}", self.context["run_id"], self.request,
                response={"transcript": self.entries}, max_bytes=_max_bytes(self.context),
            )
        self.window = ""

    def _finish(self, failed: bool) -> None:
        """Called by the runner when the run ends: write a finished window, drop one a failure cut short."""
        if failed:
            self.window = ""
        else:
            self.close()


def _take_path(source: str, take: str) -> Path:
    for label, value in (("source", source), ("take", take)):
        if not _SAFE_NAME.fullmatch(value):
            raise TapeError(f"unsafe {label} name: {value!r}")
    return TAPE_DIR / source / f"{take}.json"


def _normalize(value: Any) -> Any:
    """Return the JSON representation or raise a useful tape error."""
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise TapeError("a take must be JSON-serializable") from exc


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in _REDACTED_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
