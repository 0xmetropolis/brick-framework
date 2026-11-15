# Internal Python bricks

This repository organizes one codebase as discrete domain bricks. Bricks are used only by sibling bricks inside this repository; they are not external packages or services.

Define every domain as a named folder under `bricks/`. `example_brick` is a placeholder showing the required files and contracts, with one source port and one recorded take.

```text
bricks/<brick_name>/
├── __init__.py           # exposes only the internal run entry point
├── contract.py           # typed boundary, interface version, ports, state ownership
├── AGENTS.md             # optional domain-specific additions
├── input/                # config, adapters (one per port), tapes, input contract
├── runner/               # run entry point, IDs, ledger, optional flow smoke tests
└── src/                  # private logic and optional focused tests
```

- Rules inherit from the closest `AGENTS.md`; not every folder needs one. The `input/`, `runner/`, and `src/` folders have contracts because each owns a different boundary.
- Declare every crossing of the brick boundary as a **port** in `contract.py`, and implement it with one file of the same name under `input/adapters/`.
- `src/` reaches outside sources and sibling bricks only through its own `input/` adapters.
- Expose only `run` to sibling bricks through the brick's top-level `__init__.py`.
- Keep recorded answers under `input/tapes/<source>/<take>.json` and the run ledger under `runner/ledger/`.
- Sibling calls are fully brick-contained: they create their own run IDs and replay unless a joint follows a source (below).
- Declare every joint and its `eventual` or `orchestrated` consistency policy in `contract.py`. The declared graph must be acyclic.
- Declare stable identifiers for application state owned by the brick. One state resource has one owner.

`contract.py` contains:

```python
CONTRACT_VERSION = 1              # the interface version; joints pin it
DETERMINISTIC = True
OWNED_STATE = ("database:example-records",)
PORTS = {
    "rates": {"kind": "source", "epoch": 2, "label": {"api": "v2"}},
    "pricing": {"kind": "joint", "policy": "eventual", "pin": 1},
}

class BrickInput(TypedDict): ...
class BrickOutput(TypedDict): ...
```

Smoke tests are not required per brick. Keep them only under `runner/tests/` of an **entry** brick, with at most three files. They prove integration through `run`. Put focused domain tests under `src/tests/` only when the private logic warrants them.

Joint adapters still import only the sibling's top-level `run`; `contract.py` is not another exported API. The `run` annotations expose the boundary to static tooling, while each adapter translates into its owning brick's types.

Split a brick when `run` becomes a large dispatcher, its adapters stop being easy to understand, or unrelated changes repeatedly touch the same `src/`.

## Ports

A **port** is one crossing of the brick boundary, declared once in `PORTS` and implemented by one adapter file with the same name. There are two kinds:

| Kind | Reaches | Fields |
| --- | --- | --- |
| **source** | An outside system: an API, a database, a file, a tool, a game | `epoch` (required), `label`, `followable` |
| **joint** | A sibling brick, named for the sibling | `policy` and `pin` (both required), `follow` |

- **`epoch`** is an integer you bump when the adapter's request or its reading of the answer changes meaning. A take recorded at another epoch is stale.
- **`label`** says what the source is: a game version, a build, a protocol. It is merged into every take's request, so a take recorded against another version is stale and the error names the field. A session needs one to go fresh or record.
- **`pin`** is the sibling's `CONTRACT_VERSION` this brick was written against. `CONTRACT_VERSION` is the interface version: bump it when `BrickInput` or `BrickOutput` changes meaning. A pin that has fallen behind is a lint error, so a changed interface cannot go unnoticed.
- **`followable`** and **`follow`** are how a source goes fresh through a joint (see Source ports).

The linter ties each declaration to its adapter file, its tape folder and its imports, and finds a stale take (epoch or label) without running anything.

## Shapes

Nothing declares what kind of brick this is. The tools compute its **shape** from its ports and its files:

| Shape | Computed from | What it means |
| --- | --- | --- |
| `sealed` | `PORTS = {}` | Output is a function of input alone. It has no `input/adapters/` and no `input/tapes/` (Git cannot track an empty folder; the linter does not require them). The direct-I/O ban that `src/` already carries widens to the whole brick, plus `random` and `time`; its standard-library imports are limited to an allowlist of modules with no I/O, clock, randomness or process state (`SEALED_STDLIB` in `tools/lint_bricks.py`; third-party libraries are still allowed); and any `.now()`, `.today()` or `.utcnow()` call is an error. `runner/` is exempt because it writes ledger entries and owns `rng.py`. It is deterministic by definition, so `DETERMINISTIC = False` is an error. |
| `entry` | the brick has a `__main__.py` | Invoked as a whole operation, never imported for its parts. No joint may point at it. Its `__main__.py` may import nothing from the brick tree except the brick's own `run`. Smoke tests belong only here. |
| `open` | everything else | The regular brick. Nothing beyond the rules every brick gets. |

A brick with no ports and a `__main__.py` is routed as `entry` and held to the sealed rules too. The shape picks the prompt the review gate reads (`review/sealed.md`, `review/entry.md`, `review/open.md`, plus `review/common.md`); a repository can override one of those prompts but cannot add a shape. In prose, write "an open brick" and `open()` with its parentheses: `open` is also the builtin the linter polices in `src/`.

## Replay, fresh, and record

The public Python entry point has the shape:

```python
run(inputs, *, fresh=False, record=False)
```

`fresh` and `record` each take `False` (nothing), `True` (every source port of this brick), or a collection of port names: one of the brick's own sources (`"rates"`), a source reached through a followed joint (`"assistant.xai"`), or `"*"` for all of them. An unknown name is an error that lists the names that exist.

| You want | You write |
| --- | --- |
| Everything replayed (default) | `run(inputs)` |
| This brick's sources fresh, siblings replayed | `run(inputs, fresh=True)` |
| A fresh source, a recorded sibling | `run(inputs, fresh={"game"})` |
| A sibling's source fresh, own sources replayed | `run(inputs, fresh={"assistant.xai"})` |
| One own source recorded | `run(inputs, record={"rates"})` |
| Everything reachable fresh | `run(inputs, fresh={"*"})` |

| Per source port | Adapter behavior | Tracked take data |
| --- | --- | --- |
| replay (default) | Replay the named take | Read only |
| `fresh` | Call the real source | Unchanged |
| `record` | Call the real source, redact and validate it | Atomically replace the named take |

`record` implies fresh for the same names. A missing or mismatched take is an error, never an implicit fresh call. `True` never reaches a sibling: a sibling's source goes fresh only if you name it and both sides consent (below).

Takes use stable paths and canonical JSON, so normal runs do not churn Git. An explicit record is the review point that may create a diff. Each take is stamped with the run that recorded it (`recorded_in`); ledger entries and takes can be correlated later without involving the runner in adapter persistence.

A take is `input/tapes/<source>/<take>.json`:

```json
{"schema_version": 2, "source": "rates", "take": "usd-eur", "epoch": 2,
 "recorded_in": "20261115-...", "request": {"label": {"api": "v2"}, "...": "..."},
 "response": {"...": "..."}}
```

The brick's `CONTRACT_VERSION` is deliberately not in a take: an interface change should not force re-recording sources that did not change.

## The ledger

Every run writes one ledger entry under `runner/ledger/`. It lists the run's own `fresh` and `record` sources, the `followed` sources it passed down (`assistant.xai`), the input, the output or error, and the timestamp.

- A **tracked** entry (`<run id>.json`) is written only for a run that touched a source fresh or recorded, followed one fresh, or failed.
- A replay-only run, and every run with `BRICK_TEST=1` (smoke tests set it), writes a local, gitignored `<run id>.local.json`.
- Tracked and local entries are each bounded by `retention.entries`.

## Source ports

A source port is how a brick reaches a live source: anything outside this repository that answers calls and may hold state, such as a game or simulator, a browser, a database, a model API, a device. A *session* is a series of calls to it that belong together.

- **Replay is the default; `fresh` is the one switch to the live source.** A missing or mismatched take is an error, never an implicit fresh call.
- **One call:** `tape.once(ctx, "<source>", take, request, fetch)` records or replays one request and answer and returns `(response, stamp)`. Pass `keep_errors=False` for a paid or hard-to-repeat call, so one failed record does not replace a good take with an error.
- **A session:** `tape.session(ctx, "<source>", name)` records a chatty source as windows: one take per window (a game day, a page, a turn), operations replayed in order, so one stale window is found and re-recorded alone. The runner closes any session still open when the run ends, writing a finished window and dropping one a failure cut short.
- **The label.** A session needs `label` on its port. After connecting in `fresh` or `record`, `session.verify(seen)` compares what the source reports with the label and refuses to continue on a mismatch (`LabelMismatch`). Takes recorded before a source had a label stay unlabelled and replay until their next re-record.
- **The launcher.** The script that starts the outside process (a headless game, a browser, a database) is the launcher. It lives outside the bricks, in the repository or beside it, and a wrapper runs it around a `fresh` run. The adapter only connects to what the launcher started; it does not start or stop the process. The launcher should hand out free ports, and the adapter should check the source's version on connect.

**Following.** A sibling replays unless the run asks otherwise by name, and the sibling must have agreed. All of this must hold, and the linter checks it:

1. The sibling marks the source `followable` in its own `PORTS`.
2. The caller's joint is `orchestrated` and lists the source in `follow`.
3. The caller's adapter takes the flags from `tape.follow(ctx, "<sibling>")` and writes `run(request, fresh=flags["fresh"], record=flags["record"])`. A hard-coded `fresh=True`, a missing flag or `**kwargs` is an error.

It goes one level, and run IDs never propagate. The ledger lists the followed sources as `followed`.

*Use a source port* when the source can be driven in a controlled setting, its answers are reproducible enough that a recording stays meaningful, and the work is a conversation: simulators and games, browsers under automation, a database in a scenario test, a model API, devices behind a driver, short exchanges with a queue.

*Do not use one* for a source whose live use does real, irreversible things (payments, email, production data), whose answers differ every time and carry meaning in the difference (market prices, search results), that streams or pushes events, that returns very large payloads (a take is limited to 120 KiB and lives in Git), that several clients share at once, that runs for months, or that depends on real timing or on a person in the loop.

A session lives inside one run. A session shared by several entry bricks keeps one adapter file per brick; the launcher outside the bricks is how to avoid duplicating the process code. A session reached through a sibling joint would cost a run, and a ledger entry, per call.

## Tooling

`bricks/__init__.py` enables repository-wide standard-library test discovery. Each brick's top-level file defines its repository-internal entry point. Keep `runner/__init__.py`; add `runner/tests/__init__.py` only when that entry brick has smoke tests. `input/`, `adapters/`, and `src/` do not need package-marker files.

Once at least one entry brick has a smoke test, run all smoke tests with:

```sh
python3 -m unittest discover -s bricks -t . -v
```

Python's test runner exits with status 5 when no tests exist, which is the expected state of this boilerplate: `example_brick` has no smoke tests.

Check brick shape, import direction, public exports, ports, takes and limits with:

```sh
python3 tools/lint_bricks.py
```

Render the declared ports as Mermaid with:

```sh
python3 tools/graph_bricks.py
```

Bricks are boxes, source ports are cylinders labelled with their epoch, and an arrow is a joint: solid for `orchestrated`, dashed for `eventual`, thick when it follows sources. An entry or sealed brick says so in its label; each node lists the state its brick owns and says `nondeterministic` when the brick declares `DETERMINISTIC = False`. Bricks that no sibling depends on are drawn with a thicker border; those are the top-level flows that may carry smoke tests.

`tools/brick_contract.py` reads a brick's `contract.py` without importing it and computes its shape; repo-local checks should use `contract_value(brick, "NAME")`, `brick_ports(brick)` and `brick_shape(brick)` rather than a regular expression. Framework tests: `python3 -m unittest discover -s tools/tests -t .`.

The linter warns, and never fails, on experience problems: a mode-like environment variable in an adapter, an adapter that never plays a tape, `src/` reading or overriding the run plan, a session on a source with no label, takes near their size limit, boilerplate copies that have drifted, and smoke tests that do not mark their runs as tests.

Run any brick, naming the ports to go fresh or record, from the command line (this is how to record or refresh a brick that has no `__main__.py`):

```sh
python3 tools/run_brick.py <brick> --input request.json [--fresh [PORT ...]] [--record [PORT ...]] [--test]
```

The input is JSON from a file or stdin. `--fresh` or `--record` alone means every source port of that brick; with names it means those ports (`rates`, `assistant.xai`, `'*'`); `--record` implies fresh. Put the brick name first. `--test` keeps the ledger entry local, and a failure is one line with a nonzero status. The tool lives outside the bricks and calls only the brick's `run`.

## Moving a project to this version

A project built on the previous version converts once, as a single cut, with `tools/convert_ports.py`. There is no compatibility mode: a brick in the old format fails the lint with a pointer to the script, and old manifests and takes are never read.

```sh
python3 tools/convert_ports.py --dry-run     # converts a copy, prints what would change
python3 tools/convert_ports.py               # converts the bricks under ./bricks in place
```

It rewrites each brick's `contract.py` to `PORTS`, moves takes to schema 2 under `input/tapes`, moves `runner/runs` to `runner/ledger`, swaps in this framework's boilerplate (`tape.py`, `run.py`, `rng.py`), renames the config keys, and prints every call in your adapters and `src` that still uses an old name, with what to use instead. Change those by hand, run the linter and your tests, commit, then delete the script from the project.

| Before | After |
| --- | --- |
| `SIBLING_DEPENDENCIES`, sibling adapter | a joint port in `PORTS` |
| `LIVE_ADAPTER`, `MODE_PASSING`, `pass_mode` | `followable` on a source, `follow` on a joint, `tape.follow` |
| `LANE` (strict, pure, workflow) | the computed shape: open, sealed, entry |
| `evidence.py`, `through()`, `Transcript`, `load_example`, `save_example` | `tape.py`, `tape.once`, `tape.session`, `tape.load`, `tape.write` |
| `input/data/<adapter>/<case>.json`, `fingerprint`, `capture_run_id` | `input/tapes/<source>/<take>.json`, `label`, `recorded_in` |
| `run(..., save=True)`, saved mode | `run(..., record=...)`, replay |
| `runner/runs`, run records, `BRICK_RUN_KIND=test` | `runner/ledger`, ledger entries, `BRICK_TEST=1` |
