# Input contract

This folder owns every port of the brick: each outside source and each sibling brick it reaches.

## Configuration

- Keep brick configuration in `config.yml`.
- Keep ledger, take, and byte limits in that file.
- Never store credentials or secrets in committed configuration or takes.

## Adapters

- Create one Python file under `adapters/` for each port declared in `PORTS`, named for the port: `<source>.py` for a source, `<sibling>.py` for a joint.
- APIs, databases, filesystems, queues, clocks, and services are outside sources beyond the repository boundary.
- Other bricks in this repository are siblings, not outside sources.
- An adapter fetches or sends data; it does not contain domain logic.
- A joint adapter calls only the sibling brick's `run` entry point.
- Every adapter function that `src/` calls takes the run context first. A function that reads the disk or network without it (a cache check, an existence test) cannot know the plan and would read the real thing in replay too; the linter warns on it.
- Adapters are called by `src/` with a plain run context containing the owning brick's run ID, its plan, and resolved configuration.
- Adapters never import or call `src/` or `runner/`.
- A joint adapter never forwards the parent run ID or other run-control state; the sibling creates its own identity. It forwards `fresh` and `record` only by following: the joint is `orchestrated`, lists the sibling's `followable` sources in `follow`, and the adapter takes both flags from `tape.follow(run_context, "<sibling>")`.

## Git-stable takes

- Store committed takes at `tapes/<source>/<take>.json`, using stable human-readable source and take names. The source is the port; one adapter file records on one tape.
- The default (replay) reads the named take and fails clearly if it is absent, if it was recorded at another epoch than the port's, or if its normalized request does not match. It never falls through to a live call.
- `fresh` calls the real source and does not modify committed takes.
- `record` implies fresh, redacts sensitive fields, checks size limits, and atomically replaces the named committed take.
- A take contains `schema_version` (2), `source`, `take`, `epoch`, `recorded_in`, a normalized `request`, and exactly one of `response` or `error`. The request carries the port's `label` when it has one.
- The brick's `CONTRACT_VERSION` is not in a take. Bump the port's `epoch` when the adapter's request or its reading of the answer changes meaning, and update the `label` when what the source is changes; the linter then finds every stale take without running anything.
- Serialize JSON with sorted keys, two-space indentation, and a trailing newline. Do not store headers, credentials, cookies, tokens, or other secrets.
- Enforce `max_take_bytes` after serialization and before replacement. Keep no more than `takes_per_tape` takes for one source.
- The run that recorded a take and the ledger entry of that run can be correlated outside the execution loop through `recorded_in`; the runner does not gather adapter references.
- Takes are intentionally tracked in Git. Only an explicit record should normally change them.

## Recorded data

In replay an adapter returns what was recorded. Whatever the source put in a response (a wall-clock time, a cost, a duration, a counter) describes the recording, not this run.

- **Return the stamp in the typed response.** `tape.once` returns `(response, stamp)`: `{"recorded": True, "recorded_in": ...}` for a replay and `recorded: False` for a fresh or record run. Put the stamp beside the response, and name source-reported figures for what they are (`measured_at`, `source_reported_cost_usd`). `src/` then labels its output by data and never reads the plan. Do not add a side channel to the run context or the runner for this.
- **Do not hand-write the three modes.** `tape.once(ctx, source, take, request, fetch)` is the replay / fresh / record contract for one call; the template adapter shows it. For a paid or hard-to-repeat call pass `keep_errors=False`, so one failed record does not replace a good take with an error. For a chatty source (a session of many operations) use `tape.session(ctx, source, name)`: one take per window, operations replayed in order, and a mismatch that names the field that changed.
- **Put what changes the answer into the request, or into the label.** A take matches only its exact normalized request. Include the version of anything that changes what the source would say (a protocol or mod version, a model id, a prompt-template version) in the request or, if it describes the source as a whole, in the port's `label`, so a change fails with "recorded request does not match" instead of replaying an answer to an old question. The request is what the adapter passes to `tape.once`: it may be a stable projection of the call (purpose, day, a digest of the state, those versions) rather than a full prompt, as long as the versions stay in it. `tape.digest(state)` keeps a large state out of the request. A changed request names the fields that differ.
- **Files and tools are outside sources with a tape.** An adapter that writes files or runs a local tool still implements replay, fresh and record. Replay returns the recorded manifest (`{"path": ..., "bytes": ..., "sha256": ...}`) and neither writes the file nor runs the tool. Bytes that must be a take's content are base64 strings within `max_take_bytes`; use a digest or manifest when the bytes are not needed. An adapter that behaves the same in every mode has no replay.
- **A session is a few takes, not one huge one.** A take is one request and one response, and it is bounded by `max_take_bytes` (the linter warns at 80 percent). For a long conversation with a source, store a projection of each response (the fields `src/` uses) and split the session into takes per window that each start from a checkpoint, so one window can be re-recorded. Raise `takes_per_tape` or `max_take_bytes` in `config.yml` deliberately and say why in the same change. A run records only the windows it visits: to record a missing take, run that case with `record`; it replaces nothing else.
- **Do not grade recorded data as live.** A recorded duration, cost or counter is the recording's. A flow that checks determinism, timing or cost must say "recorded" or "not measured" on replay rather than compare two reads of the same take; run the check in a fresh run.
- **A model is its own sibling brick.** A sibling replays unless the run follows one of its sources, so a model call in its own brick is replayed while its caller runs fresh. That is how "fresh source, recorded model" is expressed. Make the model's source `followable` and list it in the caller's joint `follow` only if the caller's `fresh` should also reach the model, and spend money. Record or refresh the model's takes with `python3 tools/run_brick.py <model_brick> --input request.json --record`. Do not read the environment in an adapter to override the plan.

## Source ports

A source port drives a live source, records it on its tape, and replays by default. The README, Source ports, has the whole pattern; the rules for an adapter are:

- A missing or mismatched take is an error. Only `fresh` reaches the live source.
- A session goes through `tape.session(ctx, "<source>", name)`: one take per window, operations replayed in order. The port's `label` names what the source is (a game version, a build, a protocol) and is merged into every window request; a session needs one to go fresh or record. After connecting in `fresh` or `record`, call `session.verify(seen)` with what the source reports; it refuses to continue on a mismatch. One call goes through `tape.once`.
- Recording is decided per port by the run, never by the adapter. An adapter may only turn it off for one session: `tape.session(ctx, source, name, record=False)` keeps that session fresh when the run records the port (nothing is written, an existing take is left alone, the ledger entry lists it under `unrecorded`). `record=True`, the default, is accepted and cannot turn recording on.
- A session whose `fetch` raises, when that exception ends the run, is written as a failed take: the operations that succeeded, then the failing one last with `"error": {"type": ...}`. Replay plays them and then raises the recorded error. It is written only for that failure (not for a later bug in the brick, a failure the brick caught, fresh or replay), and never replaces an existing complete take; the lint requires that only the last operation of a transcript carries an error.
- The adapter connects to a source the launcher started. It does not start or stop the process, and it checks the source's version on connect.
- A session-shaped source (hundreds of interleaved calls, state held in a process) belongs to the brick that drives it, because a sibling's `run` is one request and one answer.
- A caller reaches a followable source only by following it, never with a hard-coded flag; the caller's ledger entry lists the sources it followed.

Bricks that exchange files should not both guess a folder name. The producer lists the folder in `OWNED_STATE` (`files:<name>`), and the consumer receives the data, or a manifest of it, through a sibling `run`.

## Effects that must go fresh

An effect a brick wants to run fresh (encode a video from the frames of a session, write a file, upload) is either an adapter of the brick itself or a followable source of a sibling. A sibling only ever replays its recorded manifest when it is called without being followed, so an encoder hidden in one would never run. Pick by shape: a one-off call is a source of a sibling brick that the caller follows; an effect tied to a session the brick is driving is an adapter of that brick, with a replay that returns a manifest and runs nothing. The sibling does the sealed part (choosing and ordering the frames, building the encoder arguments) and returns data.

Adapters may write files and run local tools; they are the only place that may. "A brick cannot write files" is never a reason to drop an output.
