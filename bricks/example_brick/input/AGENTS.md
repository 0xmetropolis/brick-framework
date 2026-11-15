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
- Every adapter function that `src/` calls takes the run context first. A function that reads the disk or network without it (a cache check, an existence test) cannot know the mode and would read the real thing in saved mode too; the linter warns on it.
- Adapters are called by `src/` with a plain run context containing the owning brick's run ID, mode, and resolved adapter configuration.
- Adapters never import or call `src/` or `runner/`.
- A sibling adapter never forwards the parent run ID or other run-control state; the sibling creates its own identity. It forwards `fresh` and `save` only by mode passing: to a sibling that declares `LIVE_ADAPTER = True`, listed in the workflow's `MODE_PASSING`, with both flags taken from `pass_mode(run_context, "<sibling>")`.

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

- **Return provenance in the typed response.** `load_example` returns the whole example, and `provenance(record)` in `evidence.py` gives `{"recorded": True, "capture_run_id": ...}` to put beside the response (fresh and save modes say `recorded: False`). Name source-reported figures for what they are (`measured_at`, `source_reported_cost_usd`). `src/` then labels its output by data and never reads the mode. Do not add a side channel to the run context or the runner for this.
- **Do not hand-write the three modes.** `through(ctx, adapter, case, request, live)` in `evidence.py` is the saved / fresh / save contract for one call and returns `(response, provenance)`; the explicit form in the template adapter is the readable reference. For a chatty source (a session of many operations) use `Transcript(ctx, adapter, case)`: one saved example per window, operations replayed in order, and a mismatch that names the field that changed.
- **Put what changes the answer into the request.** A saved example matches only its exact normalized request. Include the version of anything that changes what the source would say (a protocol or mod version, a model id, a prompt-template version), so a change fails with "saved request does not match" instead of replaying an answer to an old question. The request is what the adapter passes to `load_example`: it may be a stable projection of the call (purpose, day, a digest of the state, those versions) rather than a full prompt, as long as the versions stay in it. `digest(state)` in `evidence.py` keeps a large state out of the request. A changed request now fails naming the fields that differ.
- **Files and tools are external sources with a saved mode.** An adapter that writes files or runs a local tool still implements saved, fresh and save. Saved mode returns the recorded manifest (`{"path": ..., "bytes": ..., "sha256": ...}`) and neither writes the file nor runs the tool. Bytes that must be an example's content are base64 strings within `max_evidence_bytes`; use a digest or manifest when the bytes are not needed. An adapter that behaves the same in every mode has no saved mode.
- **A session is a few cases, not one huge one.** An example is one request and one response, and it is bounded by `max_evidence_bytes` (the linter warns at 80 percent). For a long conversation with a source, store a projection of each response (the fields `src/` uses) and split the session into cases per window that each start from a checkpoint, so one window can be re-recorded. Raise `saved_examples_per_adapter` or `max_evidence_bytes` in `config.yml` deliberately and say why in the same change. To record only a missing case, run that case with `save`; it replaces nothing else.
- **Do not grade recorded data as live.** A recorded duration, cost or counter is the capture's. A flow that checks determinism, timing or cost must say "recorded" or "not measured" on replay rather than compare two reads of the same example; run the check in a fresh run.
- **A model is its own sibling brick.** A sibling replays unless it is a Live Adapter its workflow passes the mode to, so a model call in its own brick is replayed while its caller runs fresh. That is how "fresh source, recorded model" is expressed; make the model brick a Live Adapter and list it in `MODE_PASSING` only if the workflow's `fresh` should also reach the model, and spend money. Record or refresh the model's examples with `python3 tools/run_brick.py <model_brick> --input request.json --save`. Do not read the environment in an adapter to override the mode.

## Live Adapters

A **Live Adapter** drives a live source over a session, records it through `Transcript` or `through()`, and replays by default. The README, Live Adapters, has the whole pattern; the rules for an adapter are:

- A missing or mismatched saved example is an error. Only `fresh` reaches the live source.
- A session goes through `Transcript(ctx, adapter, case, fingerprint=...)`: one saved example per window, operations replayed in order. The fingerprint names what the source is (a game version, a build, a protocol) and is merged into every window request. After connecting in `fresh` or `save`, call `transcript.check_source(observed)` with what the source reports; it refuses to continue on a mismatch. One call goes through `through()`, optionally with `fingerprint=`.
- The adapter connects to a source the launcher started. It does not start or stop the process, and it checks the source's version on connect.
- A session-shaped source (hundreds of interleaved calls, state held in a process) is an adapter of the workflow that drives it, because a sibling's `run` is one request and one answer. A one-off call is a Live Adapter brick (`LIVE_ADAPTER = True`) that a workflow may pass its mode to.
- A workflow reaches a Live Adapter sibling live only by mode passing, never with a hard-coded flag; the workflow's run record lists the siblings that ran fresh.

Bricks that exchange files should not both guess a folder name. The producer lists the folder in `OWNED_STATE` (`files:<name>`), and the consumer receives the data, or a manifest of it, through a sibling `run`.

## Effects that must go live

An effect a workflow wants to run live (encode a video from the frames of a session, write a file, upload) is either an adapter of the workflow itself or a Live Adapter sibling. A plain sibling only ever replays its recorded manifest when a workflow calls it, so an encoder hidden in one would never run. Pick by shape: a one-off call is a Live Adapter brick (`LIVE_ADAPTER = True`) the workflow passes its mode to; an effect tied to a session the workflow is driving is an adapter of the workflow, with a saved mode that replays a manifest and runs nothing. The sibling does the pure part (choosing and ordering the frames, building the encoder arguments) and returns data.

Adapters may write files and run local tools; they are the only place that may. "A brick cannot write files" is never a reason to drop an output.
