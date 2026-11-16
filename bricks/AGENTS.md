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
- `contract.py` declares `CONTRACT_VERSION` (the interface version), typed `BrickInput` and `BrickOutput`, `PORTS` and `OWNED_STATE`. It may declare `DETERMINISTIC` (meaning: the same input and the same adapter answers give the same output; what an adapter returns is an input to `src/`, so a brick that calls a live source may say `True`. `True`: `src/` takes the clock, randomness and generated IDs as input and the linter holds it to that; `False`: nondeterminism on purpose; omitted: no promise, no check). It declares nothing about what kind of brick it is: the shape is computed from the ports and the files (see the README).
- `contract.py` is not a second public surface. Joint adapters import only `run`; its annotations carry the boundary types, and the adapter translates into its owning brick's types.
- `PORTS` maps each port name to its declaration. A **source** port (`epoch`, optional `label`, optional `followable`) reaches an outside system. A **joint** port, named for the sibling (`policy`, `pin`, optional `follow`), reaches a sibling brick. `eventual` accepts independently committed state and possible lag. `orchestrated` means the importing, dependent brick is responsible for sequencing and compensation; it does not imply a distributed transaction. `pin` is the sibling's `CONTRACT_VERSION` this brick was written against.
- For a sealed sibling the policy has nothing to say (it has no state), so declare `eventual`; `orchestrated` is for a sibling whose effects the importing brick must sequence or compensate.
- Logic that several bricks need goes in a sealed brick, reached over a joint. Anything else inside a brick's own files is the author's business: the framework compares nothing between bricks.
- The declared joint graph must be acyclic. A cycle is a signal to merge responsibilities or introduce a parent brick.
- `OWNED_STATE` lists stable application-resource identifiers. No identifier may be owned by two bricks. A brick that reads or writes a resource another brick owns (a folder, a file a path in `config.yml` points at) does not do it behind the owner's back: it asks the owner through a joint `run`, or the resource has one owner and the others receive its content as input. A path in configuration that names another brick's owned folder is a missing joint. Local takes in `input/tapes/` and entries in `runner/ledger/` do not need to be listed.
- `runner/` creates the run context, calls private `src/` logic, and writes the run's ledger entry.
- `input/` owns configuration, the adapters (one per port), and the tapes.
- `src/` owns private domain logic and may call its own `input/` adapters when it needs data or an external effect.
- Execution-call direction is `runner -> src -> input adapter`. `runner` does not call adapters directly, and adapters never call back into `src` or `runner`.
- `runner` passes the caller-supplied brick input unchanged into `src` along with a plain run context containing the brick's run ID, its plan (which source ports are fresh or recorded), and resolved configuration; `src` passes that context into adapters.
- Each adapter owns replay, fresh and record behavior for its port, through `tape.once` or `tape.session`.
- `src` may import adapters from its own brick, but it may not directly import external clients or sibling bricks.
- A sibling brick may be reached only through a joint adapter that calls the sibling's `run` entry point, in a file named for the sibling.
- Every joint adapter must correspond to a declared joint, and every declared port has exactly one adapter file.
- Run identity never propagates across a brick boundary, and neither does `fresh` or `record`, with one exception named following: a joint that is `orchestrated` may list in `follow` the sources the sibling marked `followable`, and its adapter takes the flags from `tape.follow(run_context, "<sibling>")`. Every other sibling call uses ordinary inputs, no `fresh` or `record`, and replays. See the README, Source ports.
- Do not import another brick's `src/`, `runner/`, configuration, or tapes.
- Do not package bricks for, or expose them to, consumers outside this repository.

A `BrickInput` field named `op`, `flow`, `action`, `operation` or `kind` is the split signal: one run is one operation, so each value becomes its own brick (a sealed brick included). The linter warns on such a field, and on a `total=False` input with more than six fields.

## The process entry

An entry brick's `__main__.py` is not a place for logic. It parses arguments, calls `run` once with `fresh` and `record` taken from flags, prints, and writes the files `run` returned. It makes no decisions about the result beyond the exit status (for example, `0` when `passed` is true), computes nothing, and imports no I/O module other than `os` and `pathlib` (the linter warns). A failure that is a missing or stale take should print its one-line message and exit with a status, not a traceback. An effect it would need (an encoder, an upload) is an adapter of the entry brick. To run a brick that has no `__main__.py`, use `python3 tools/run_brick.py`.

A sealed brick has no ports, so it has no `input/adapters/` and no `input/tapes/`; every brick with ports requires `input/adapters/`, and every brick with a source requires `input/tapes/`.

## Adding a brick

1. `cp -r bricks/example_brick bricks/<name>`; delete `input/tapes/example_source/` and clear `runner/ledger/*.json`; set `config.yml`'s `brick:`.
2. Edit `contract.py`: `DETERMINISTIC`, `PORTS`, owned state, the two TypedDicts.
3. Write `src/logic.py`'s `execute`. Add one adapter per port under `input/adapters/`, named for the port; a source adapter plays its tape with `tape.once` or `tape.session`.
4. A sealed brick has `PORTS = {}`, so it also deletes `input/tape.py`, `input/adapters/` and `input/tapes/`.
5. Run `python3 tools/lint_bricks.py`.

Do not add `__init__.py` merely to mark folders. Keep the repository `bricks/` file for discovery, the brick-level file for the internal entry point, and `runner/__init__.py`. Add `runner/tests/__init__.py` only when an entry brick has smoke tests. The other folders use namespace-package behavior.
