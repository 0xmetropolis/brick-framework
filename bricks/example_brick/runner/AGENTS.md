# Runner contract

This folder owns orchestration, brick-local run identity, the run ledger, and smoke tests.

## Brick entry point

- `run.py` contains the brick's only repository-internal entry point: `run`. The template's `run.py` and `rng.py` are working code, copied with the brick like `input/tape.py`; only the imports of `execute` and the contract are brick-specific, so copies stay identical.
- Annotate `run` with the brick's `BrickInput` and `BrickOutput` types from `contract.py`.
- `run` accepts ordinary inputs plus keyword-only `fresh` and `record` options. Each takes `False`, `True` (every source port of this brick), or port names (`"rates"`, `"assistant.xai"`, `"*"`); `record` implies fresh for the same names, and an unknown name is an error.
- `run` creates a run ID through `rng.py`, resolves configuration, turns `fresh` and `record` into the run's plan, builds a plain run context, calls `src/`, closes whatever the run left open, writes the ledger entry, and returns the result.
- `run` passes its caller-supplied brick input unchanged into `src` with the run context.
- `runner/` does not call input adapters directly and does not collect adapter-result references. Adapter calls and takes belong to `src/` and `input/`. The one thing it calls on their behalf is a close function an adapter registered in `run_context["closers"]` (a session), in a `finally`, so a failed run leaves no half-written window.
- Run identity and the plan are brick-local. They are not forwarded when an adapter invokes a sibling brick, except the part of the plan a joint follows, through `tape.follow`.
- Do not expose adapters or `src/` functions to sibling bricks.

## The ledger

- Write one ledger entry per run under `ledger/`, using its brick-local run ID.
- A **tracked** entry (`<run id>.json`) is written only for a run that touched a source fresh or recorded, followed one fresh, or failed. Every other run, and every run with `BRICK_TEST=1`, writes a local `<run id>.local.json`: still bounded (its own retention), but gitignored by `ledger/.gitignore`, so replays and tests do not churn tracked history.
- Each entry includes the run ID, the brick's own `fresh` and `record` sources, the `followed` sources passed down (`<joint>.<source>`), the input, the output or error, and the timestamp.
- Takes recorded by an explicit record operation carry the same run ID (`recorded_in`) and can be joined outside the runner.
- `run` may be called from several threads at once (an entry brick's threads calling one sibling). Writing and pruning must tolerate an entry another call already removed.
- Retain only the configured number of recent entries (`retention.entries`), tracked and local separately. Prune after writing the new entry.
- Enforce `max_entry_bytes` before writing an entry. A value that does not fit is summarized, not dropped: long strings and lists become a size, a digest and a head, bytes become a size and digest, and the entry lists what it summarized. Inputs and outputs are JSON values; carry bytes as a base64 string or a manifest.
- Tracked entries are intentionally in Git (local ones are not). A brick called very often by siblings, such as a sealed brick, never writes a tracked entry unless it fails, so its history stays quiet; calling it once per tick is still better avoided. The human or agent operating the repository commits the resulting bounded history changes.

## Tests

- Smoke tests prove integration through the public brick boundary. Focused `src` tests prove domain correctness.
- `runner/tests/` is optional and reserved for entry bricks (bricks with a `__main__.py`): the flows invoked as a whole, which no other brick depends on.
- Do not add smoke tests to every brick. An entry brick may have at most three smoke-test files, each defining an explicit input and calling only the brick's top-level `run`.
- Smoke tests set `BRICK_TEST=1` in `setUp` (the linter warns when they do not), so even a fresh or failing run leaves only a local entry. Default tests replay takes. A fresh test affects only its own brick; it never makes sibling runs fresh other than through a declared follow.
- Use a record test only when deliberately replacing reviewed committed takes.
- Focused tests under `src/tests/` are optional and may directly test private domain logic. Organize them only when the logic warrants it.
- If `runner/tests/` exists, keep its `__init__.py` so sibling bricks retain distinct test import paths during discovery.
