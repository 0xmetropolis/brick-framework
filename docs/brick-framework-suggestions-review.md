---
cursor:
  subagentId: "bc-09a7e8a7-92e4-58f6-a6d6-597552f15f18"
---

# Experience improvements from a downstream port

Source: an agent ported a game-automation harness onto this framework and wrote ten suggestions (`internal/brick-framework-suggestions.md` in that project's store, written against `main` at `004ef44`). An adversarial review kept three fixes as proposed and rejected the other designs. John's direction: the rejected designs are not a friction log. Each one points at a real agent or developer experience (AX/DX) problem, and each problem is solved here in the form that fits it best: **code** (a lint, helper or runner change), **docs** (guidance a reader needs), or **prompt** (agent-facing instructions in `AGENTS.md`). The fixes do not copy the suggested designs. They keep the framework's principles: one `run` per brick, lanes, acyclic siblings, owned state, replay by default, lint-enforced guarantees, stdlib only.

Base: the fork's `main` (`0a2067b`). Nothing is taken from other branches. `evaluation-system` and `workflow-orchestrated-modes` are cited only as prior art.

## 1. Fixes, by problem

Form: **C** code, **D** docs, **P** prompt (`AGENTS.md` instructions). The column "Why this form" is the deciding reason.

| # | Problem the agent hit | Form and why | Fix | Where |
|---|---|---|---|---|
| S4.1 | A clean clone of a pure brick failed lint: `input/adapters` was required, the pure lane forbids adapters, and Git cannot hold an empty folder. | **C**: a rule contradicted itself, so the rule is wrong. | `input/adapters` is optional for the pure lane only. | `lint_bricks.py` (`LANE_OPTIONAL`); tests |
| S4 | Pure brick copied from the template fails on `evidence.py` imports with no hint what to do. | **C**: the error is the moment of need. | The pure-lane error now says "delete input/evidence.py". | `lint_pure` |
| S4 | No scaffold: the agent wrote a private `mkbrick`. | **P**, not code: `AGENTS.md` bars generators, and the real need is a checklist of what to edit and delete. | "Adding a brick" steps, including what a pure brick deletes. | `bricks/AGENTS.md` |
| S4 | The template runner was a stub, so six bricks hand-wrote the same ~90 lines (config reader, record, bounding, pruning), with a copy that drifted. | **C**: working code beats prose. It stays a per-brick copy, not an import, like `evidence.py`. | `runner/run.py` and `rng.py` are working: run ID, nested config reader, run context, mode, failure recording, byte bounding, pruning. | `example_brick/runner/`; `test_runner.py` |
| S4, S1 | Byte-identical boilerplate copies (`game_socket.py` x3, runner x6) drifted silently. | **C**: drift is detectable; guidance cannot see it. Warn-only. | `# BOILERPLATE: <id>@<n>` marker; the linter warns on same-version copies that differ, and names a lagging copy. Template files are marked. | `lint_copies`; template files |
| S4 | `assigned_literal` copied in lint and graph; a repo-local lint had no way to read a contract. | **C** | `tools/brick_contract.py` with `assigned_literal` and `contract_value(brick, NAME)`; both tools use it. | `tools/brick_contract.py` |
| S1 | Three flows each needed the live game, so each had a copy of its adapter. A sibling cannot go live (mode never crosses a brick boundary). | **D** and **C** (message): the principle stays, but the agent needs to know the two legal shapes. | Guidance "Sources several bricks share": one brick owns the source (step-shaped `run`) for replayed flows; a flow that must go live keeps its own marked adapter. Forwarding `fresh` or `save` now says why it is rejected and what to do. | `input/AGENTS.md`; `lint_python` message |
| S2 | A 331-turn session was one 254 KB example; the cap was raised; one extra call forced a full re-record; stale transcripts were not flagged; a mismatch said only "does not match". | **C** for the messages, **D** for the shape. | `load_example` names the differing fields (bounded), the capture run and the fix, and lists existing cases when one is missing. Oversize and too-many-cases errors say what to do. Lint warns at 80 percent of the limit. Guidance: project responses, cases per window, versions in the request. | `evidence.py`; `lint_records`; `input/AGENTS.md` |
| S3 | "Live game, recorded model" in one brick led to an env-var switch no record shows. | **C** (the hack is detectable) and **D** (the right shape is a sibling brick, which always replays). | Warn on an adapter that reads an env var named like `REPLAY`/`FRESH`/`SAVE`/`MODE`/`LIVE`/`OFFLINE`/`RECORD`. Guidance: a model is its own sibling brick. | `env_switch`; `input/AGENTS.md` |
| S5 | A model answer could not be replayed: the exact prompt text had to match; recording cost real money; no cheap way to add one case. | **D**: exact matching must stay; the adapter already chooses the request, so the fix is knowing how. | Guidance: a stable projection (purpose, day, state digest, versions) as the request; `digest()` helper; record a missing case by running only that case with `save`. | `input/AGENTS.md`; `evidence.digest` |
| S6 | File and tool adapters (`ffmpeg`, result files) behaved the same in every mode; nothing flagged an adapter with no saved mode. | **C**: the framework could not tell them from one that forgot. Guidance for what a saved mode is. | Warn when a non-sibling adapter never calls `load_example`. Guidance: a saved mode returns a recorded manifest and writes or runs nothing; bytes are base64 within limits. | `lint_python`; `input/AGENTS.md` |
| S6 | Two bricks shared a `frame_dir` string: an undeclared file channel. | **D**: a lint cannot see folder names that two bricks agree on. | Producer lists the folder in `OWNED_STATE`; consumer receives data or a manifest through a sibling `run`. | `input/AGENTS.md` |
| S7 | Recorded durations and costs printed as this run's; a determinism check that cannot fail on replay; `src/` forcing `mode="fresh"`. | **C** for the detectable, **D** for the habit. No new channel through the runner or context. | Warn on `src/` reading the mode (adopted earlier) and on `dict(ctx, mode=...)`. `provenance(record)` helper for the typed response. Guidance: do not grade recorded data as live. | `lint_python`; `evidence.provenance`; `input/AGENTS.md` |
| S8 | Run-record churn: smoke tests wrote tracked records; pure bricks called per tick filled history. | **C** for tests (a mechanism removes the churn), **D** for the rest. | `BRICK_RUN_KIND=test` writes a local, gitignored `*.test.json` with its own retention; test records do not count toward `retention.runs`; smoke tests that never mention `BRICK_RUN_KIND` warn. Guidance: `retention.runs: 1` for hot pure bricks. | `runner/run.py`; `runs/.gitignore`; `lint_records`, `lint_smokes`; `runner/AGENTS.md` |
| S9 | Intentional nondeterminism was a string in config, read by a regex in a repo-local lint. | **C**: a declaration the linter enforces. | `DETERMINISTIC` in `contract.py`: `True` holds `src/` to no `random`, `time`, `threading`, `uuid`, `secrets` or clock calls; `False` declares intent; omitted makes no promise. A pure brick may not say `False`. The graph marks nondeterministic bricks. The template declares `True`. | `lint_deterministic`; `graph_bricks.py`; template contract |
| S10 | `Image.open(io.BytesIO(data))` was flagged as file access, so decoding was rewritten. | **C**: the false positive is narrow and safe to remove. | `x.open(io.BytesIO(...))` (or `StringIO`) with the buffer built in the call is allowed; any other `open` is still flagged, and the message says what is allowed. | `opens_a_file`; `src/AGENTS.md` |

## 2. Commits on `fit-fixes` after `f10d3e5`

| Commit | Change |
|---|---|
| `ed7bf9a` | Shared contract reader for lint and graph. |
| `97302c7` | `DETERMINISTIC` in `contract.py`, enforced. |
| `c426cc9` | Adapter and `src` experience lints: env switch, no saved mode, mode override, in-memory `open`, forwarding message. |
| `b7e083f` | Warn on drifting `BOILERPLATE` copies. |
| `3490a83` | `evidence.py`: field-level diffs, case listing, hints, `digest`, `provenance`; 80 percent size warning. |
| `f6c5144` | Working template runner, with local test records. |
| `f537951` | Test records outside retention; smoke tests must mark themselves. |
| `4d29026` | Pure-lane hint to delete `evidence.py`. |
| `b4e7eaf` | Docs and prompts: Adding a brick, Sources several bricks share, sessions, determinism, runner. |

Earlier on this branch (kept): `9d8c0b0` pure lane needs no `input/adapters`; `0350da9` warn-only mode-read lint; `e0db0b1` the in-memory `src` sentence and the "Recorded data" docs.

## 3. What code could not fix

- **A flow going live through a sibling (S1).** Mode never crossing a brick boundary is a core guarantee. The framework now explains it and shows both legal shapes, but a live-driving flow still keeps its own adapter. `codex/workflow-orchestrated-modes` is the upstream design for lifting this and is outside this change.
- **Keyed or fuzzy replay (S5).** Not built: it would let a recorded answer reach a different prompt. The adapter can already choose a stable request, so this is guidance.
- **Blobs (S6).** Not needed: small files are base64 in an example within the existing size limit.
- **Re-recording a long session (S2).** Still needs the live source. The fixes make it cheaper (cases per window) and make staleness loud, not free.
- **Cost of calling a pure brick every tick (S8).** A brick-shape choice. `retention.runs: 1` bounds the churn.
