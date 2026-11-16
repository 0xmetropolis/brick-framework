# Source contract

This folder owns the brick's private domain logic. Its internal organization is otherwise unrestricted.

- Receive the brick input and a plain run context from `runner/`.
- Decide when outside or sibling-brick data is needed and call adapters from this brick's `input/` folder.
- Pass the run context into every adapter so it can apply replay, fresh, or record behavior.
- Do not implement mode branching here; each adapter owns that behavior using the plan in the run context. The linter warns when `src/` reads `plan` from the run context or builds a context with another one (`dict(ctx, plan=...)`). To use one source fresh and another recorded, name the port in `run(..., fresh=...)`; to keep one always replayed, make it its own sibling brick.
- The import ban is a floor, not a loophole: `src/` may not use `importlib`, `__import__`, `eval`, `exec`, `sys.modules` or `__builtins__` to reach a module it could not import by name.
- Do not perform direct network, database, filesystem, subprocess, environment, clock, or sibling-brick calls. Those calls must go through adapters.
- Do not import sibling bricks directly. Import only this brick's input adapters. Do not bind the `bricks` package itself (`import bricks`, `import bricks as b`, `import bricks.<this brick>` without `as`) either: it reaches every sibling by attribute.
- Do not import code from elsewhere in the repository outside `bricks/` (a top-level `helpers.py` or `shared/` folder). A brick's code lives in the brick; code shared by several bricks is a sealed brick reached over a joint. The linter finds a top-level `.py` file or Python folder in the repository root; it cannot see an installed package, a module reached through `sys.path` or `PYTHONPATH`, a nested folder, or a file that shadows a standard-library name. Adapters are not checked: they are where outside access lives.
- Return values, not files. Render, encode and decode in memory (`io.BytesIO`, `bytes`, `str`) and hand the result back as data; adapters write files and run external tools. The `open` check is name-based: any call named `open` in `src/` is a file access, except `x.open(io.BytesIO(data))` (or `StringIO`) with the buffer built in the call, which cannot reach a file. A buffer held in a variable is still flagged.
- Libraries are allowed only when their use is local and does not make external calls.
- Keep behavior deterministic for the same supplied inputs unless randomness is explicitly passed in. Declare `DETERMINISTIC = True` in `contract.py` and the linter holds `src/` to it (no `random`, `time`, `threading`, `uuid`, `secrets`, `.now()`); declare `False` for nondeterminism on purpose. A brick with no ports is sealed and the linter holds the whole brick to this: no clock calls, no `random` or `time`, no adapters at all.
- Export functions only for use by this brick's `runner/`; the repository-internal brick entry point remains `run` alone.
- Focused tests here prove domain rules and invariants. They are distinct from the few top-level smoke tests that prove integration.
- Split the brick when unrelated domain responsibilities make this folder change together repeatedly.
