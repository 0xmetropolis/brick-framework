# Domain brick contract

Each direct child of `bricks/` is one discrete domain brick. Rules inherit from the repository contract; a brick-level `AGENTS.md` is optional unless that brick adds domain-specific rules.

Keep `bricks/__init__.py` so the standard-library test runner can discover smoke tests across every brick from the repository root.

## Required shape

```text
<brick_name>/
├── __init__.py
├── contract.py
├── input/
│   └── AGENTS.md
├── runner/
│   └── AGENTS.md
└── src/
    └── AGENTS.md
```

## Boundaries

- The brick's top-level `__init__.py` exposes only the repository-internal `run` entry point from `runner/run.py`.
- `contract.py` declares `CONTRACT_VERSION`, typed `BrickInput` and `BrickOutput`, `SIBLING_DEPENDENCIES`, and `OWNED_STATE`. It may declare `LIVE_ADAPTER` (strict lane: the brick drives a live source, records through `Transcript` or `through()`, and replays by default) and, for a workflow, `MODE_PASSING` (the Live Adapter siblings it passes its mode to). It may declare `DETERMINISTIC` (meaning: the same input and the same adapter answers give the same output; what an adapter returns is an input to `src/`, so a brick that calls a live source may say `True`. `True`: `src/` takes the clock, randomness and generated IDs as input and the linter holds it to that; `False`: nondeterminism on purpose; omitted: no promise, no check). It may also declare `LANE`: `strict` (the default), `pure`, or `workflow`. A lane only adds rules; see the README.
- `contract.py` is not a second public surface. Sibling adapters import only `run`; its annotations carry the boundary types, and the adapter translates into its owning brick's types.
- `SIBLING_DEPENDENCIES` maps each sibling name to `eventual` or `orchestrated`. `eventual` accepts independently committed state and possible lag. `orchestrated` means the importing, dependent brick is responsible for sequencing and compensation; it does not imply a distributed transaction.
- For a pure sibling the policy has nothing to say (it has no state), so declare `eventual`; `orchestrated` is for a sibling whose effects the importing brick must sequence or compensate.
- The declared sibling graph must be acyclic. A cycle is a signal to merge responsibilities or introduce a parent brick.
- `OWNED_STATE` lists stable application-resource identifiers. No identifier may be owned by two bricks. A brick that reads or writes a resource another brick owns (a folder, a file a path in `config.yml` points at) does not do it behind the owner's back: it asks the owner through a sibling `run` and declares the dependency, or the resource has one owner and the others receive its content as input. A path in configuration that names another brick's owned folder is a missing dependency. Local evidence in `input/data/` and `runner/runs/` does not need to be listed.
- `runner/` creates the run context, calls private `src/` logic, and records the run outcome.
- `input/` owns configuration, external-source adapters, sibling adapters, and saved adapter examples.
- `src/` owns private domain logic and may call its own `input/` adapters when it needs data or an external effect.
- Execution-call direction is `runner -> src -> input adapter`. `runner` does not call adapters directly, and adapters never call back into `src` or `runner`.
- `runner` passes the caller-supplied brick input unchanged into `src` along with a plain run context containing the brick's run ID, mode, and resolved configuration; `src` passes that context into adapters.
- Each adapter owns saved, fresh, and save behavior.
- `src` may import adapters from its own brick, but it may not directly import external clients or sibling bricks.
- A sibling brick may be reached only through a sibling adapter that calls the sibling's `run` entry point.
- Every sibling adapter must correspond to a declared sibling dependency.
- Run identity never propagates across a brick boundary, and neither does the mode, with one exception named mode passing: a workflow may pass its mode to a sibling that declares `LIVE_ADAPTER = True`, only if it lists that sibling in `MODE_PASSING` (an `orchestrated` dependency) and takes the flags from `pass_mode(run_context, "<sibling>")`. Every other sibling call uses ordinary inputs, no `fresh` or `save`, and the sibling's default saved mode. See the README, Live Adapters.
- Do not import another brick's `src/`, `runner/`, configuration, or saved data.
- Do not package bricks for, or expose them to, consumers outside this repository.

A `BrickInput` field named `op`, `flow`, `action`, `operation` or `kind` is the split signal: one run is one operation, so each value becomes its own brick (a pure brick included). The linter warns on such a field, and on a `total=False` input with more than six fields.

## The process door

A workflow's `__main__.py` is a door, not a place for logic. It parses arguments, calls `run` once with `fresh` and `save` taken from flags, prints, and writes the files `run` returned. It makes no decisions about the result beyond the exit status (for example, `0` when `passed` is true), computes nothing, and imports no I/O module other than `os` and `pathlib` (the linter warns). A failure that is a missing or stale saved example should print its one-line message and exit with a status, not a traceback. An effect the door would need (an encoder, an upload) is an adapter of the workflow. To run a brick that has no door, use `python3 tools/run_brick.py`.

A `pure` brick has no adapters, so it omits `input/adapters/`; every other lane requires the folder.

## Adding a brick

1. `cp -r bricks/example_brick bricks/<name>`; clear `runner/runs/*.json`; set `config.yml`'s `brick:`.
2. Edit `contract.py`: `LANE`, `DETERMINISTIC`, dependencies, owned state, the two TypedDicts.
3. Write `src/logic.py`'s `execute`. Add one adapter per source under `input/adapters/`, each with a saved mode (`load_example`).
4. A `pure` brick also deletes `input/evidence.py`, `input/adapters/` and the example adapter; it has no sources.
5. Run `python3 tools/lint_bricks.py`. Keep boilerplate copies (`# BOILERPLATE:` files) identical to the template unless you bump `@n` on purpose.

Do not add `__init__.py` merely to mark folders. Keep the repository `bricks/` file for discovery, the brick-level file for the internal entry point, and `runner/__init__.py`. Add `runner/tests/__init__.py` only when a `workflow` brick has smoke tests. The other folders use namespace-package behavior.
