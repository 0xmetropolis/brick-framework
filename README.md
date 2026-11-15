# Internal Python bricks

This repository organizes one codebase as discrete domain bricks. Bricks are used only by sibling bricks inside this repository; they are not external packages or services.

Define every domain as a named folder under `bricks/`. `example_brick` is an empty placeholder showing the required files and contracts.

```text
bricks/<brick_name>/
├── __init__.py           # exposes only the internal run entry point
├── contract.py           # typed boundary, dependencies, state ownership
├── AGENTS.md             # optional domain-specific additions
├── input/                # config, adapters, saved examples, input contract
├── runner/               # run entry point, IDs, history, optional flow smoke tests
└── src/                  # private logic and optional focused tests
```

- Rules inherit from the closest `AGENTS.md`; not every folder needs one. The `input/`, `runner/`, and `src/` folders have contracts because each owns a different boundary.
- Add one file under `input/adapters/` for every external source and sibling brick.
- `src/` reaches external sources and sibling bricks only through its own `input/` adapters.
- Expose only `run` to sibling bricks through the brick's top-level `__init__.py`.
- Keep named adapter examples under `input/data/<adapter>/<case>.json` and recent runs under `runner/runs/`.
- Sibling calls are fully brick-contained: they create their own run IDs and always use their own default saved mode.
- Declare every sibling dependency and its `eventual` or `orchestrated` consistency policy in `contract.py`. The declared graph must be acyclic.
- Declare stable identifiers for application state owned by the brick. One state resource has one owner.

`contract.py` contains:

```python
CONTRACT_VERSION = 1
LANE = "strict"
DETERMINISTIC = True
SIBLING_DEPENDENCIES = {"another_brick": "eventual"}
OWNED_STATE = ("database:example-records",)

class BrickInput(TypedDict): ...
class BrickOutput(TypedDict): ...
```

Smoke tests are not required per brick. Keep them only under `runner/tests/` of a `workflow` brick, with at most three files. They prove integration through `run`. Put focused domain tests under `src/tests/` only when the private logic warrants them.

Sibling adapters still import only the sibling's top-level `run`; `contract.py` is not another exported API. The `run` annotations expose the boundary to static tooling, while each adapter translates into its owning brick's types.

Split a brick when `run` becomes a large dispatcher, its adapters stop being easy to understand, or unrelated changes repeatedly touch the same `src/`.

## Lanes

A lane is the enforcement class a brick declares for itself with `LANE` in `contract.py`. Every rule above applies to every brick; a lane only adds rules, so no brick can opt out of the boundary. A brick that declares no `LANE` is `strict`.

| Lane | What it means | What the linter adds |
| --- | --- | --- |
| `strict` | The regular brick. | Nothing beyond the rules every brick gets. |
| `pure` | Output is a function of input alone. | No adapters and no sibling dependencies, so it has no `input/adapters/` folder (Git cannot track an empty one; the linter does not require it for this lane). The direct-I/O ban that `src/` already carries widens to the whole brick, plus `random` and `time`, and its standard-library imports are limited to an allowlist of modules with no I/O, clock, randomness or process state (`PURE_STDLIB` in `tools/lint_bricks.py`; third-party libraries are still allowed), plus any `.now()`, `.today()`, or `.utcnow()` call. `runner/` is exempt because it records runs and owns `rng.py`; `datetime` stays importable for annotations and arithmetic. |
| `workflow` | Invoked as a whole operation, never imported for its parts. | No contract may name it as a sibling dependency. It alone may carry a process door, `__main__.py`, and that door may import nothing from the brick tree except the brick's own `run`. Smoke tests belong only here. |

A lane cannot exist without an enforcer: the `LANES` table in `tools/lint_bricks.py` maps each name to the function that checks it, and the linter rejects any other value.

## Saved, fresh, and save

The public Python entry point has the shape:

```python
run(inputs, *, fresh=False, save=False)
```

| Option | Adapter behavior | Tracked example data |
| --- | --- | --- |
| default | Replay the named saved example | Read only |
| `fresh=True` | Call the real source | Unchanged |
| `save=True` | Call the real source, redact and validate it | Atomically replace the named example |

`save=True` implies a fresh call. A missing or mismatched saved example is an error, never an implicit live call. Neither option propagates when a sibling adapter calls another brick, except by mode passing to a Live Adapter sibling (below).

Examples use stable paths and canonical JSON, so normal runs do not churn Git. An explicit save is the review point that may create a diff. Each saved example includes the capture run ID; run records and evidence can be correlated later without involving the runner in adapter persistence.

## Live Adapters

A **Live Adapter** drives a live source over a session, records it through `Transcript` or `through()`, and replays by default.

A *live source* is anything outside this repository that answers calls and may hold state: a game or simulator, a browser, a database, a model API, a device. A *session* is a series of calls to it that belong together.

- **Replay is the default; `fresh` is the one switch to the live source.** A missing or mismatched saved example is an error, never an implicit live call.
- **How it records.** `through()` records one call. `Transcript` records a session as windows: one saved example per window (a game day, a page, a turn), operations replayed in order, so one stale window is found and re-recorded alone.
- **The source fingerprint.** A `Transcript` requires `fingerprint=` (a game version, a build, a protocol). It is merged into every window's request, so a window recorded against another version is stale and the error names the field. When the adapter connects in `fresh` or `save`, `check_source(observed)` compares what the source reports with the fingerprint and refuses to continue on a mismatch (`SourceMismatch`).
- **The launcher.** The script that starts the outside process (a headless game, a browser, a database) is the launcher. It lives outside the bricks, in the repository or beside it, and a wrapper runs it around a `fresh` run. The adapter only connects to what the launcher started; it does not start or stop the process. The launcher should hand out free ports, and the adapter should check the source's version on connect.

Where a Live Adapter lives depends on the shape of the source:

- **A one-off call** (a model call, an encode, a write): one request and one answer. Make it a **Live Adapter brick**: strict lane, `LIVE_ADAPTER = True` in `contract.py`, with an adapter that records through `through()` or `Transcript`. A workflow may pass its mode to it (next section).
- **A session** (hundreds of interleaved calls, a process holding state, a decision between calls): keep the adapter inside the workflow that drives it. A brick's `run` is one request and one response, so a session as a sibling would mean a run record and a saved example per call.

**Mode passing.** A sibling always replays, with one exception, which has its own name: a workflow may pass its mode to a Live Adapter sibling. All of this must hold, and the linter checks it:

1. The sibling declares `LIVE_ADAPTER = True` (strict lane; an adapter that records through `through()` or `Transcript`).
2. The workflow lists it in `MODE_PASSING`, and it is an `orchestrated` dependency.
3. The workflow's adapter takes the flags from `pass_mode(run_context, "<sibling>")` and writes `run(inputs, fresh=flags["fresh"], save=flags["save"])`. A hard-coded `fresh=True`, a missing flag or `**kwargs` is an error.

`saved` passes nothing; `fresh` passes `fresh`; `save` passes both. Pure siblings, `eventual` siblings and every call from a brick that is not a workflow still replay. It goes one level: a Live Adapter does not pass the mode on. Run IDs never propagate. The workflow's run record lists the siblings that ran fresh as `fresh_siblings`.

*Use a Live Adapter* when the source can be driven in a controlled setting, its answers are reproducible enough that a recording stays meaningful, and the work is a conversation: simulators and games, browsers under automation, a database in a scenario test, a model API (one completion as a Live Adapter brick; a multi-turn chat as a session), devices behind a driver, short exchanges with a queue.

*Do not use one* for a source whose live use does real, irreversible things (payments, email, production data), whose answers differ every time and carry meaning in the difference (market prices, search results), that streams or pushes events, that returns very large payloads (a saved example is limited to 120 KiB and lives in Git), that several clients share at once, that runs for months, or that depends on real timing or on a person in the loop.

`bricks/__init__.py` enables repository-wide standard-library test discovery. Each brick's top-level file defines its repository-internal entry point. Keep `runner/__init__.py`; add `runner/tests/__init__.py` only when that workflow has smoke tests. `input/`, `adapters/`, and `src/` do not need package-marker files.

Once at least one workflow has a smoke test, run all smoke tests with:

```sh
python3 -m unittest discover -s bricks -t . -v
```

Python's test runner exits with status 5 when no tests exist, which is the expected state of this empty boilerplate.

Check brick shape, import direction, public exports, and evidence limits with:

```sh
python3 tools/lint_bricks.py
```

Render the declared sibling-dependency graph as Mermaid with:

```sh
python3 tools/graph_bricks.py
```

Each node lists the state its brick owns, and says `nondeterministic` when the brick declares `DETERMINISTIC = False`. A solid arrow is an `orchestrated` dependency and a dashed arrow is an `eventual` one. Bricks that no sibling depends on are drawn with a thicker border; those are the top-level flows that may carry smoke tests.

`tools/brick_contract.py` reads a brick's `contract.py` without importing it; repo-local checks should use `contract_value(brick, "NAME")` rather than a regular expression on config. Framework tests: `python3 -m unittest discover -s tools/tests -t .`.

The linter warns, and never fails, on experience problems: a mode-like environment variable in an adapter, an adapter with no saved mode, `src/` reading or overriding the run mode, evidence near its size limit, boilerplate copies that have drifted, and smoke tests that do not mark their records as tests.

Run any brick, in any mode, from the command line (this is how to record or refresh a brick that has no process door, since only a workflow may carry `__main__.py`):

```sh
python3 tools/run_brick.py <brick> --input request.json [--fresh | --save] [--test]
```

The input is JSON from a file or stdin, `--save` implies fresh, `--test` writes a local test record, and a failure is one line with a nonzero status. The tool lives outside the bricks and calls only the brick's `run`.
