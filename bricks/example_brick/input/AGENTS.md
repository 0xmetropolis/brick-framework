# Input contract

This folder owns every external-source boundary and sibling-brick boundary for the brick.

## Configuration

- Keep brick configuration in `config.yml`.
- Keep run, saved-example, and byte limits in that file.
- Never store credentials or secrets in committed configuration or saved evidence.

## Adapters

- Create one Python file under `adapters/` for each external source or sibling brick.
- APIs, databases, filesystems, queues, clocks, and services are external sources beyond the repository boundary.
- Other bricks in this repository are sibling bricks, not external sources.
- An adapter fetches or sends data; it does not contain domain logic.
- A sibling adapter calls only the sibling brick's `run` entry point.
- Adapters are called by `src/` with a plain run context containing the owning brick's run ID, mode, and resolved adapter configuration.
- Adapters never import or call `src/` or `runner/`.
- A sibling adapter never forwards `fresh`, `save`, the parent run ID, or other run-control state. The sibling run remains fully contained and creates its own identity.

## Git-stable saved examples

- Store committed examples at `data/<adapter>/<case>.json`, using stable human-readable adapter and case names.
- Default saved mode reads the named example and fails clearly if it is absent or its normalized request does not match. It never falls through to a live call.
- `fresh=True` calls the real source and does not modify committed examples.
- `save=True` implies a fresh call, redacts sensitive fields, checks size limits, and atomically replaces the named committed example.
- A saved example contains `schema_version`, `contract_version`, `adapter`, `case`, `capture_run_id`, normalized `request`, and exactly one of `response` or `error`.
- Refuse to replay an example captured under a different brick contract version.
- Serialize JSON with sorted keys, two-space indentation, and a trailing newline. Do not store headers, credentials, cookies, tokens, or other secrets.
- Enforce `max_evidence_bytes` after serialization and before replacement. Keep no more than `saved_examples_per_adapter` cases for one adapter.
- The capture run and its saved adapter evidence can be correlated outside the execution loop through `capture_run_id`; the runner does not gather adapter references.
- Saved examples are intentionally tracked in Git. Only an explicit save operation should normally change them.

## Recorded data

In saved mode an adapter returns what was recorded. Whatever the source put in a response (a wall-clock time, a cost, a duration, a counter) describes the capture, not this run.

- **Return provenance in the typed response.** `load_example` returns the whole example, including `capture_run_id`. Translate it into the adapter's response (for instance `{"recorded": mode == "saved", "capture_run_id": ...}`), and name source-reported figures for what they are (`measured_at`, `source_reported_cost_usd`). `src/` then labels its output by data and never reads the mode. Do not add a side channel to the run context or the runner for this.
- **Put what changes the answer into the request.** A saved example matches only its exact normalized request. Include the version of anything that changes what the source would say (a protocol or mod version, a model id, a prompt-template version), so a change fails with "saved request does not match" instead of replaying an answer to an old question. The request is what the adapter passes to `load_example`: it may be a stable projection of the call (purpose, day, a digest of the state, those versions) rather than a full prompt, as long as the versions stay in it.
- **Files and tools are external sources with a saved mode.** An adapter that writes files or runs a local tool still implements saved, fresh and save. Saved mode returns the recorded manifest (`{"path": ..., "bytes": ..., "sha256": ...}`) and neither writes the file nor runs the tool. Bytes that must be an example's content are base64 strings within `max_evidence_bytes`; use a digest or manifest when the bytes are not needed. An adapter that behaves the same in every mode has no saved mode.
- **A model is its own sibling brick.** A sibling always runs in its default saved mode, so a model call in its own brick is replayed while its caller runs fresh. That is how "live source, recorded model" is expressed. Record or refresh the model's examples by running that brick directly with `save`. Do not read the environment in an adapter to override the mode.
