---
cursor:
  subagentId: "bc-09a7e8a7-92e4-58f6-a6d6-597552f15f18"
---

# Review of "Brick framework suggestions", against `main`

Reviewed doc: `internal/brick-framework-suggestions.md` in the Project store, by "Syx Engineer", written against `jtlacci/brick-framework` `main` at `004ef44`. This review is against the fork's `main` (`0a2067b`: upstream `004ef44` plus the pstack install hook). Nothing from other branches is assumed. Two unmerged upstream branches are mentioned only as prior art or a possible future direction, because John has said they are not ready:
- `evaluation-system`: replaces saved mode with `ctx["io"]` capture and replay and a separate evaluation runner.
- `codex/workflow-orchestrated-modes`: lets a workflow pass `fresh`/`save` to orchestrated, non-pure siblings.

Terms:
- **Adopted**: implemented on branch `fit-fixes`, off `main`.
- **Friction**: something the porting agent hit that the framework does not change for. Each friction below says what the agent hit, why the framework rejects the change, and the in-framework approach on `main`.

## 1. Outcome table

| # | Suggestion | Verdict on `main` | Status |
|---|---|---|---|
| S1 | Shared adapters (`bricks/__adapters__`) | Friction, only partly solvable on `main`. Keep copies, or split the game into a sibling brick for replay. | rejected |
| S2 | Tapes: chained, segmented session evidence | Friction. Fingerprint inside the request; project the response; cases per window; raise per-brick caps in config. | rejected |
| S3 | Per-adapter modes | Friction. Model as its own sibling brick (a sibling always runs saved). | rejected |
| S4 | Boilerplate fixes | Item 1 adopted (`9d8c0b0`). Scaffold, reference runner, drift marker, `_contract.py` are frictions. | split |
| S5 | Keyed turns, `record_missing` | Friction. Exact matching stays; semantic request with versions; model brick. | rejected |
| S6 | Adapter kinds and blobs | Friction. Manifest and base64 idioms documented (`e0db0b1`). Kinds and blobs rejected. | docs only |
| S7 | Replay honesty | Warn-only lint (`0350da9`) plus provenance-in-response idiom (`e0db0b1`). `run_context["replay"]` and `replayed_from` rejected. | adapted |
| S8 | `RECORD` policy, test-run directory | Friction. `runs: 1`; call pure bricks less often. | rejected |
| S9 | `DETERMINISM` trait | Friction. Split the orchestrator; Syx adds its own constant. | rejected |
| S10 | `src` no-`open` lint, in-memory idiom | Docs sentence adopted (`e0db0b1`). Lint refinement is a friction. | docs only |

## 2. What was implemented on `fit-fixes`

| Commit | Change |
|---|---|
| `9d8c0b0` | S4.1: `input/adapters` is optional for the `pure` lane only (`LANE_OPTIONAL` in `tools/lint_bricks.py`). Four tests: a pure brick without the folder lints clean; strict and workflow still require it; a pure brick still may not gain adapters. README and `bricks/AGENTS.md` updated. |
| `0350da9` | S7, warn-only: `src/` reading `mode` from `ctx`, `context` or `run_context` (subscript or `.get`) warns. Adapters read the mode by design and are not flagged; a request field named `mode` is not flagged. Three tests. |
| `e0db0b1` | Docs. `src/AGENTS.md`: the new warning, and the S10 sentence (return values, not files; render in memory; the `open` check is name-based). `input/AGENTS.md`, new section "Recorded data": provenance in the typed response, version fingerprints in the request, files and tools keep a saved mode through manifest examples with base64 bytes, a model is its own sibling brick. |

This review itself is committed as `docs/brick-framework-suggestions-review.md`; see `git log` for its commit.

## 3. Adaptations

- **S7 provenance.** On `main` the adapter knows the mode and `load_example` returns the whole example including `capture_run_id`. So the idiom is that the adapter returns provenance in its typed response (for instance `{"recorded": mode == "saved", "capture_run_id": ...}`) and `src/` labels output by data. There is no `run_context["replay"]` list and no `replayed_from`.
- **S7 lint.** Only the key `mode` is flagged, only in `src/`. The doc's `modes` and `replay` keys do not exist on `main`.
- **S10.** On `main` `io` is not in `DIRECT_IO`, so the original `io.BytesIO` idiom stands. The sentence also says the `open` check is name-based, so a call named `open` is flagged even on an in-memory buffer.
- **S6 lint dropped.** A warn-only lint for "adapter has no saved-mode path" was not in scope. I did not add it.
- **S5 and S2 share one idiom.** Both are served by putting versions in the request, and S5's semantic-request option is one sentence in that bullet.
- **S3 and S1 depend on one `main` fact.** A sibling brick always runs in its default saved mode, which is exactly what the model brick wants and exactly what the game brick does not.
- **Base.** The pstack hook (`0a2067b`) is part of the base, not of `fit-fixes`.

## 4. Frictions

### S1. Three byte-identical game adapters
- **What the agent hit.** `game_socket.py` (416 lines) is copied into `measured_run`, `orchestrator` and `test_scenario`. Fixes were applied to every copy by script. `result_files.py`, `ruleset_file.py` and `xai_chat.py` were duplicated too, and two `xai_chat.py` copies already differ.
- **Why the framework rejects a shared-adapter import.** A cross-brick import that is not `run` is the shared runtime `AGENTS.md` forbids ("Do not add shared runtimes, generators ..."). The shared module would sit outside every brick and the linter walks only `brick.rglob`, so none of the adapter rules would apply to it. I tested: `bricks.__adapters__.x` is rejected today ("sibling adapter may import only run", "sibling dependency ... not declared"). The proposal's `bind(__name__)` also cannot receive the evidence functions "passed in", as its text says.
- **In-framework approach on `main`, with its limit.**
  - A `game_client` strict brick with a step-shaped operation (session handle, commands and ticks in, state out) is one `run`, one request and one response, so it fits the contract. Flows would reach it through thin sibling adapters.
  - The limit: on `main` a sibling always runs saved, and mode never crosses a brick boundary. So a workflow's `--fresh` cannot make the game live through that sibling. A `game_client` serves replay and evaluation (flows replay it), and it is recorded or refreshed by running it directly with `save`. A flow that must drive the live game still needs the adapter in its own brick.
  - So the honest `main` position is: keep the copies for flows that go live, change them together by script, and keep the number of game-touching bricks small. That is the framework's current cost, not a defect to fix by sharing.
- **Prior art.** `codex/workflow-orchestrated-modes` (workflow to orchestrated non-pure sibling, both `fresh` and `save` explicit) is the upstream answer to exactly this limit. If it merges, the `game_client` brick becomes complete.
- **Not adopted, available on request.** A warn-only drift marker (`# BOILERPLATE: id@n`) would detect copy divergence. It is heavier enforcement than `AGENTS.md` allows without John's request.

### S2. Whole-session transcripts as one example
- **What the agent hit.** One saved example per session (254 KB, 331 turns). `max_evidence_bytes` was raised per brick (600,000 and 400,000). Replay is positional, so one extra call forced a full re-record against a licensed game, and stale transcripts were not flagged.
- **Why the framework rejects tapes.** Tapes need a new evidence layout (I confirmed `lint_records` rejects `input/data/<a>/<dir>/x.json` and the required field set), hash chains, checkpoints, optional delta encoding and a budget declared in `contract.py`. That is heavier enforcement and a second evidence format. The cap is a reviewed per-brick config number, so a brick raising it is visible in a diff, which is the design.
- **In-framework approach on `main`.**
  - Staleness: put the protocol, mod and adapter version in the normalized request. `load_example` already fails on a request mismatch, so a stale transcript fails with "saved request does not match".
  - Size: have the adapter store a projection of `state` and `rooms` (what `src/` uses), not raw responses. Most of the 254 KB is near-duplicate state.
  - Re-record cost: split a session into cases per window that start from a game checkpoint, so one window is re-recorded. Raise `saved_examples_per_adapter` and `max_evidence_bytes` in `config.yml`, with the reason in the diff.
  - Documented in `input/AGENTS.md`, "Recorded data".
- **Prior art.** `evaluation-system` records every exchange with a `seq` number outside Git and leaves replay to a runner. That removes the cap problem, but it is not on `main`.

### S3. "Live game, recorded model" and the env-var hack
- **What the agent hit.** One mode per run, so `xai_chat.py` read `SYX_XAI_REPLAY` to replay the model while the game ran live. Syx-side code, **unverified** here.
- **Why the framework rejects per-adapter modes.** `run(inputs, *, fresh, save)` is one mode per brick, and the mode is an explicit, unforwarded option. Narrowing it per adapter adds a new type, a new context field `src/` could read (which S7 then has to ban), and a `mixed` state in every record.
- **In-framework approach on `main`.** Put `xai_chat` in its own sibling brick. A sibling always runs saved, so a workflow run fresh replays the model while the game adapter in the workflow goes live. Refresh the model's examples by running that brick directly with `save`. Delete the env-var switch. Documented in "Recorded data".
- **Prior art.** `codex/workflow-orchestrated-modes` would let a workflow carry its mode to orchestrated I/O siblings. Declare the model sibling `eventual` and it still stays saved, so this brick shape works with or without it.

### S4 (rejected parts). Scaffolder, reference runner, drift marker, shared `assigned_literal`
- **What the agent hit.** A private `mkbrick` helper; six byte-identical 86-line `runner/run.py` copies with a hand-rolled YAML reader; `input/evidence.py` identical in four bricks; `xai_chat.py` drifting between two; `assigned_literal` duplicated in `lint_bricks.py` and `graph_bricks.py`.
- **Why the framework rejects them.** `AGENTS.md`: "Do not add shared runtimes, generators, dependencies, or heavier enforcement unless the user explicitly requests them." The example `runner/run.py` is a stub by design. A drift marker is a convention that does nothing when ignored.
- **In-framework approach on `main`.** Copy `example_brick`; keep boilerplate copies identical by script when one changes; keep bricks few. Two copies of a small AST helper in `tools/` is a cost John can accept or pay later.
- **Prior art.** `evaluation-system` moves the runner into one `_runtime.py`. That removes the copy problem differently, and is not on `main`.

### S5. Escalation answers cannot replay
- **What the agent hit.** `escalation-no-food-day9` was a missing example, so a workaround flag and the env hack were used. The first saved plan failed exact match because the logged prompt embedded state JSON in a different key order from the planner brick. Run-to-run nondeterminism (since fixed in Syx) made matching impossible. A replayed answer carries its recorded cost.
- **Why the framework rejects keyed matching.** `match: key` returns a recorded answer to a different prompt, so a prompt regression passes. The doc's own guarantee score for it was -1. It also depends on S2, S3, S6 and S7, a four-deep chain of unadopted changes. Exact matching of the normalized request is the only reason a replay means anything.
- **In-framework approach on `main`.** The request is what the adapter passes to `load_example`, so the adapter chooses it. Pass a stable projection: purpose, day, a digest of the projected state, model id and prompt-template version. A template-version bump fails closed with "saved request does not match". Put the model in its own sibling brick (S3). Each missing escalation is a normal `save` of that single case, with `saved_examples_per_adapter` raised in config. Recorded cost is a source-reported figure: name it so (S7).

### S6. Files, folders and frames
- **What the agent hit.** Four adapters (`artifact_files`, `ffmpeg_encoder`, `result_files`, `template_files`) behave the same in every mode because files could not be examples. Saved mode touches the disk and runs `ffmpeg`, half the observability brick is skipped for lack of frames, and a `frame_dir` string passes between sibling bricks as an undeclared file-system channel.
- **Why the framework rejects adapter kinds and blobs.** A kind such as `tool` or `scratch` that exempts an adapter from replay is a back door: the default path could reach `ffmpeg` or the disk, which `review/strict.md` criterion 2 blocks. `subprocess` is in `DIRECT_IO`, so a "no network imports" rule for `tool` would not cover the real effect. A blob store needs its own layout (`lint_records` accepts only `<adapter>/<case>.json`), size budget and `.gitignore` handling.
- **In-framework approach on `main`.** A file or tool adapter still implements saved, fresh and save. Saved mode returns a recorded manifest `{path, bytes, sha256}` and writes nothing and runs nothing. Small frames are base64 strings in an example: a 640x360 PNG is about 40 KB, so about 55 KB encoded, under the 120 KiB default. The `frame_dir` channel should be one brick, or an `OWNED_STATE` resource with an explicit sibling call. Documented in "Recorded data".
- **Not adopted.** A warn-only lint for an adapter that never references `load_example`/`save_example` and imports no sibling would catch an adapter with no saved mode. It was not in scope.

### S7 (rejected parts). Replay provenance in the run context and runner
- **What the agent hit.** Recorded CPU and cost printed as this run's numbers; a determinism check that cannot fail in replay printing "IDENTICAL"; `src/` forcing `mode="fresh"` into the context (`dict(ctx, mode="fresh")`). Syx-side, **unverified** here.
- **Why the framework rejects `run_context["replay"]` and `replayed_from`.** The list is a new adapter-to-runner write-back channel against "the runner does not gather adapter references". `replayed_from` gives `src/` sanctioned, replay-dependent output, which is mode branching by another name and contradicts "deterministic for the same supplied inputs". `load_example` already returns `capture_run_id`.
- **In-framework approach on `main`.** The adapter returns provenance in its typed response; `src/` labels by data; the new warning covers `src/` reading the mode. A flow that grades its own determinism in replay should report "not measured", or the determinism check should be a separate fresh-only flow. Documented in "Recorded data".

### S8. Run-record churn
- **What the agent hit.** Every `run` writes a tracked JSON and prunes, including pure bricks called per decision cycle; records embed full inputs; smoke tests and replays dirty the tree (about 25 files under `runner/runs/` in one commit). The example runner never sets `kind: "test"`, but it is a stub.
- **Why the framework rejects a `RECORD` field.** "Record every run" is a core visibility rule (`runner/AGENTS.md`). A per-brick opt-out lets a brick hide runs, and a gitignored test directory hides evidence from reviewers.
- **In-framework approach on `main`.** Set `runs: 1` in a pure brick's `config.yml` (the minimum the linter accepts). Avoid calling a pure brick through a sibling adapter once per tick; that is a brick-shape cost the doc itself calls "expensive by design". Commit the bounded run history as the contract says.
- **Prior art.** `evaluation-system` removes tracked run records and uses a local capture spool; not on `main`.

### S9. Deliberate nondeterminism in `orchestrator`
- **What the agent hit.** `orchestrator` runs several instances at speed 250 and is non-deterministic on purpose. The exemption was `deterministic: false` in `config.yml`, matched by regex in a repo-local `lint_determinism.py`, which is then called with an expression that works by accident (`and` returns its right operand). **Unverified** here.
- **Why the framework rejects a `DETERMINISM` trait.** `main` enforces no determinism beyond the `pure` lane, so the field would have no consumer except one repo's lint. `lint_contract` already ignores constants it does not know, so the repo can declare its own. Per-adapter case limits are already per-brick config.
- **In-framework approach on `main`.** Split `orchestrator` into `throughput`, `bench` and `world_templates` (`AGENTS.md`: split a `run` that dispatches). Have the repo-local lint read a constant from `contract.py` with the same AST helper the linter uses, not a regex on config.
- **Prior art.** `evaluation-system` defines `DETERMINISTIC` in `contract.py` (default `False`; `True` means the output can be recomputed). If Syx adds a constant, use that name and polarity so a later migration is free.

### S10 (rejected part). Sharpening the `src/` no-open lint
- **What the agent hit.** `Image.open(io.BytesIO(data))` is flagged by the name-based `open` check, so `imaging.py` was rewritten to decode with `ImageFile.Parser().feed(data)`.
- **Why the framework rejects the change.** An exemption for `.open(BytesIO(...))` widens a lint that is already only a floor. It does not catch `img.save(path)` or `plt.savefig(path)`, and a variable (`buf`) would still be flagged. The rewrite touched one helper and produced pure bytes-in, bytes-out renderers, which is the intent of the rule.
- **In-framework approach on `main`.** The docs sentence in `src/AGENTS.md`: render in memory, return data, and use an in-memory decoder not named `open`.

## 5. Claim checks on the original doc

Verified against code on `main`:
- `REQUIRED` demands `input/adapters` while `lint_pure` forbids adapters (S4.1).
- The `src` `open` check is name-based (S10).
- There was no lint for mode reads in `src` (S7).
- `assigned_literal` is duplicated in `lint_bricks.py` and `graph_bricks.py` (S4).
- `lint_records` rejects any evidence path that is not `<adapter>/<case>.json` (S2).
- `__`-prefixed folders are skipped by `lint_repo` but not linted (S1).

Wrong or imprecise:
- "The example runner never sets `test`" is vacuous: the example `run.py` is a stub.
- S1's `bind(__name__)` cannot also receive the evidence functions "passed in".
- On `main` a pure brick copied from the example also fails on `input/evidence.py` importing `os` and `pathlib`, so it must delete that file; the test fixture already does.

Unverified (Syx-side, not checkable here): file sizes and line numbers, `SYX_XAI_REPLAY`, `lint_determinism.py` and its `exempt()` expression, the 331-turn tape, and the transcript citations.
