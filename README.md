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
- Every way of importing a sibling other than `from bricks.<sibling> import run` in its joint adapter is a linter error, including binding the `bricks` package itself (`import bricks`, `import bricks as b`). A brick's code lives in the brick. `src/` may not import code from elsewhere in the repository outside `bricks/` (the linter errors); code that several bricks need is a sealed brick, reached over a joint. The linter looks in the repository root (a `.py`, `.pyw` or `.pyc` file; an extension module, meaning any `name.so` or `name.pyd` whichever Python version or platform it was built for, such as `name.cpython-312-x86_64-linux-gnu.so`, `name.abi3.so` or `name.cp311-win_amd64.pyd`; or a folder that is a package or holds Python files) and one level inside each root folder that is not itself a package (`lib/util.py`, `src/mypkg/`), and skips hidden, vendored and build folders. It cannot see: an installed package; a module on the import path only through `PYTHONPATH`, a `.pth` file or project configuration, unless it sits in those places; code more than one level below a root folder; a repository file that shadows a standard-library name; `pickle` or `unittest.mock`, which import by string; a computed attribute name such as `getattr(sys, name)`; and `sys` reached through another module's attributes. Adapters are left alone. In `src/`, `runpy`, `pkgutil`, `pydoc`, `zipimport`, `imp` and `site`, and `sys.path`, `sys.modules`, `sys.meta_path`, `sys.path_hooks` (also through `import sys as s`, `getattr(sys, "modules")`, `vars(sys)` and `from sys import modules`) are errors for the same reason `importlib` is.
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

### The diagram

Draw any brick project, its own or yours, with one command:

```sh
python3 tools/graph_bricks.py --root <project> [--format mermaid|dot] [--output FILE]
```

It prints Mermaid (which GitHub renders in a Markdown file) by default, or Graphviz DOT with `--format dot`. It reads only each brick's `contract.py` (parsed, never imported or run) and the names of its files: no execution, no network, no timestamp, no path of this machine. Bricks, ports and edges are sorted, so **the same project always gives the same bytes**; a test runs the tool repeatedly over a multi-brick project and fails on any byte difference.

- Every brick is a box labelled with its computed shape: `sealed`, `entry` or `open`, followed by the state it owns and `nondeterministic` when it declares `DETERMINISTIC = False`.
- Every source port is a cylinder with its epoch, its label (as sorted `key=value` pairs) and `followable` when it is.
- A joint is an arrow to the sibling labelled with its policy and pin: solid for `orchestrated`, dashed for `eventual`.
- A follow is a thick arrow from the brick to each source its joint follows.
- Bricks that no sibling depends on get a thicker border (`stroke-width` in Mermaid, `penwidth=3` in DOT): those are the top-level flows. A joint to a brick that does not exist is drawn as a dashed `missing` box.
- **A legend** closes every diagram, in both formats: a `Legend` subgraph (Mermaid) or cluster (DOT) drawn with the very same node shapes, arrows and styles, explaining the shape line (`sealed`, `entry`, `open`), the owned-state and `nondeterministic` lines, the source cylinder with `epoch N`, `label k=v` and `followable`, the plain arrow that joins a brick to its source, the solid (`orchestrated`) and dashed (`eventual`) joint arrows with their pin, the thick `follows` arrow, the thick border, and the dashed `missing` box. The legend lists exactly what the renderers use and is the same for every project; a test extracts every node shape, arrow, class and attribute from the diagram and from the legend and fails if the two sets differ.

Names and labels are escaped for Mermaid, and ids are prefixed and cleaned, so a brick called `end` or `a-b.c` cannot break the diagram.

### The transcript report

Follow one piece of sample data through a project, hop by hop:

```sh
python3 tools/transcript_brick.py <brick> --root <project> --input sample.json [--name NAME] [--out DIR]
                                  [--fresh [PORT ...]] [--record [PORT ...]]
```

`<brick>` is the brick the sample enters (usually an entry brick) and `sample.json` is one input record for it (or `-` for stdin). The tool runs that brick once through its own `run`, with every source answering from its tape, and writes down each place the value passes through, in order: the entry brick with the sample, each joint crossing into a sibling (what crossed, then what the sibling returned), each source port touched (the request, the recorded response and the take file used), and the entry brick's output. It works on any brick project and needs no change to the bricks.

Replay is the default, so nothing reaches a live source unless you pass `--fresh` or `--record` with port names (as for `run_brick.py`). The run's ledger entry is local and gitignored (`BRICK_TEST=1`). Two files are written, with the same hops:

```text
<out>/<brick>-<name>.md     a Mermaid sequenceDiagram and a hop table
<out>/<brick>-<name>.json   the hops, for tests and tools
```

`--out` defaults to `<project>/reports/transcripts` and `--name` to the input file's name (`stdin` for `-`), so the path is predictable. The Markdown has a `sequenceDiagram` with one participant per brick (in the order the sample first touched them) and one per source port touched, and one arrow per hop labelled with the joint or port, its policy and pin or its epoch and label, and the take used. Under it is a table with the columns `#`, `where` (a brick, or brick to port, with its shape), `how it got here` (sample input, joint from X with policy and pin, source epoch and label, returned, output), `tape take` (the take path, or none) and `value`. The value is the **full** value at that hop, with sorted keys and no truncation or digest: inline when it is short, pretty-printed in a `<details>` block when it is long; a source hop shows its request and its response. A failed run is still reported, ending in the hop where it raised, and the command exits 1.

Replaying the same project and input gives the same bytes: the report carries no run ID, timestamp or path of this machine, and a test runs it twice, and over a copy of the project at another path, and fails on any difference. The flow is followed in the order it runs, so a brick that calls a sibling from several threads is not reported in a fixed order.

`tools/brick_contract.py` reads a brick's `contract.py` without importing it and computes its shape; repo-local checks should use `contract_value(brick, "NAME")`, `brick_ports(brick)` and `brick_shape(brick)` rather than a regular expression. Framework tests: `python3 -m unittest discover -s tools/tests -t .`.

The linter warns, and never fails, on experience problems: a mode-like environment variable in an adapter, an adapter that never plays a tape, `src/` reading or overriding the run plan, a session on a source with no label, takes near their size limit, a boilerplate file (`input/tape.py`, `runner/run.py`, `runner/rng.py`) behind the framework's version, and smoke tests that do not mark their runs as tests.

Run any brick, naming the ports to go fresh or record, from the command line (this is how to record or refresh a brick that has no `__main__.py`):

```sh
python3 tools/run_brick.py <brick> --input request.json [--fresh [PORT ...]] [--record [PORT ...]] [--test]
```

The input is JSON from a file or stdin. `--fresh` or `--record` alone means every source port of that brick; with names it means those ports (`rates`, `assistant.xai`, `'*'`); `--record` implies fresh. Put the brick name first. `--test` keeps the ledger entry local, and a failure is one line with a nonzero status. The tool lives outside the bricks and calls only the brick's `run`.

## Moving a project to this version

A project built on the previous version moves over in one cut, by hand. There is no compatibility mode: a brick in the old format fails the lint with a message that says what the new format is and points here, and old manifests, takes and run records are never read. Do the steps below for every brick, then lint and test; merge only when both are clean.

Start from this framework's files: `tools/`, `review/`, the `AGENTS.md` files, and the boilerplate files of `bricks/example_brick` (`input/tape.py`, `runner/run.py`, `runner/rng.py`, each marked `# BOILERPLATE: <id>@<n>`; the linter warns when a brick's copy is behind the framework's version). Then, for each brick:

1. **Boilerplate.** Delete `input/evidence.py`. Copy `input/tape.py` (not for a brick that will have no ports), `runner/run.py` and `runner/rng.py` from `bricks/example_brick`. Rename the keys in `input/config.yml`: `retention.runs` to `retention.entries`, `saved_examples_per_adapter` to `takes_per_tape`, `max_evidence_bytes` to `max_take_bytes`, `max_run_record_bytes` to `max_entry_bytes`.
2. **One adapter file per port.** Each outside source gets `input/adapters/<source>.py`, named for the port; each sibling the brick calls gets `input/adapters/<sibling>.py`, named for the sibling and importing only that sibling's `run`. Delete any adapter helper that is not a port (move it into the adapter that uses it).
3. **The manifest.** In `contract.py`, delete `SIBLING_DEPENDENCIES`, `LIVE_ADAPTER`, `MODE_PASSING` and `LANE`, and declare `PORTS`:
   - a source port per outside-source adapter: `{"kind": "source", "epoch": 1}`; add `"label"` with the old fingerprint if the source had one (a session needs it to go fresh or record), and `"followable": True` if the brick was a Live Adapter;
   - a joint port per sibling: `{"kind": "joint", "policy": <the old policy>, "pin": <that sibling's CONTRACT_VERSION>}`; if the old brick passed its mode to that sibling, add `"follow": (<the sibling's followable source names>,)`;
   - `PORTS = {}` for a brick with no adapters. That brick is sealed: it also loses `input/tape.py`, `input/adapters/` and `input/tapes/`, and the sealed rules apply to the whole brick.
   - A workflow keeps its `__main__.py` and is now an entry brick; a brick that had `LANE = "workflow"` but no `__main__.py` is an open brick, and its smoke tests need one.
4. **Call sites.** In adapters and `src/`, change the old names (table below). The linter holds the rest: it finds a sibling run that gets `fresh` or `record` outside a declared `follow`, an adapter that names another port's tape, a session on a source with no label.
5. **Takes.** Move `input/data/<adapter>/<case>.json` to `input/tapes/<source>/<take>.json`, where `<source>` is the adapter file's name. Inside, rename `adapter` to `source`, `case` to `take`, `capture_run_id` to `recorded_in`, and the request's `fingerprint` key to `label`; delete `contract_version`; add `"epoch": 1`; set `"schema_version": 2`. Keep the JSON canonical (sorted keys, two-space indent, trailing newline). The lint then checks each take against its port's epoch and label.
6. **The ledger.** Delete `runner/runs/`. Create `runner/ledger/` with a `.gitignore` containing `*.local.json` and a `.gitkeep`; old records are history and are not carried over. Set `BRICK_TEST=1` where smoke tests set `BRICK_RUN_KIND=test`.
7. **Check.** Run `python3 tools/lint_bricks.py`, your tests, and one replay of each flow.

| Before | After |
| --- | --- |
| `SIBLING_DEPENDENCIES`, sibling adapter | a joint port in `PORTS` |
| `LIVE_ADAPTER`, `MODE_PASSING`, `pass_mode` | `followable` on a source, `follow` on a joint, `tape.follow` |
| `LANE` (strict, pure, workflow) | the computed shape: open, sealed, entry |
| `evidence.py`, `through()`, `Transcript`, `load_example`, `save_example` | `tape.py`, `tape.once`, `tape.session`, `tape.load`, `tape.write` |
| `provenance()`, `check_source`, `SourceMismatch`, `EvidenceError` | `tape.stamp`, `tape.verify`, `LabelMismatch`, `TapeError` |
| `input/data/<adapter>/<case>.json`, `fingerprint`, `capture_run_id` | `input/tapes/<source>/<take>.json`, `label`, `recorded_in` |
| `run(..., save=True)`, saved mode | `run(..., record=...)`, replay |
| `runner/runs`, run records, `BRICK_RUN_KIND=test` | `runner/ledger`, ledger entries, `BRICK_TEST=1` |
