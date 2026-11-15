# Repository contract

This repository uses Python file boilerplate to organize one codebase as discrete domain bricks.

## Terminology

- The **repository boundary** separates this repository from APIs, databases, files, services, and other outside systems. Only these are called external.
- A **brick boundary** separates one brick from its sibling bricks inside this repository.
- A **brick entry point** is `run`. It is repository-internal and is called by joint adapters.
- A **port** is one declared crossing of a brick boundary, implemented by one adapter file of the same name. A **source** port reaches a system outside the repository; a **joint** port reaches a sibling brick through that brick's `run`.
- A **tape** is what a source port has recorded; a **take** is one recorded file on it. A source's **epoch** and **label** say when its takes go stale.
- The **ledger** is the run log under `runner/ledger/`: one entry per run.
- A brick's **shape** is computed, never declared: `sealed` (no ports), `entry` (has a `__main__.py`), or `open` (everything else).
- The modes are **replay** (the default), **fresh** (call the live source) and **record** (fresh, and write the take).

## Purpose

- Optimize for human and agent visibility, not framework machinery.
- A brick may be complex inside `src/`, but its inputs and recent runs must make its behavior understandable.
- Keep this repository as boilerplate. Do not add shared runtimes, generators, dependencies, or heavier enforcement unless the user explicitly requests them.
- Keep enforcement in `tools/lint_bricks.py` lightweight and standard-library-only. Experience checks there are warnings: they say what to do instead.
- A file copied into every brick (`input/tape.py`, `runner/run.py`, `runner/rng.py`) is boilerplate, not a shared runtime: it carries `# BOILERPLATE: <id>@<n>` and each brick owns its copy.

## Non-negotiable rules

- Put every domain brick in its own named folder under `bricks/`.
- Every brick has `input/`, `runner/`, and `src/`.
- Every brick declares a typed input and output boundary and its interface version (`CONTRACT_VERSION`) in `contract.py`.
- A brick exposes only its `run` entry point to sibling bricks.
- Every crossing of a brick boundary is a port declared in `PORTS`: outside access crosses a source port's adapter; sibling-brick access crosses a joint port's adapter.
- A source is replayed by default and goes fresh only when the run names its port (`run(..., fresh=...)`, `record=...`). The only way a run's `fresh` or `record` reaches a sibling's source is a joint's `follow` of a source the sibling marked `followable`; everything else a sibling does replays.
- Every joint declares its consistency policy as `eventual` or `orchestrated` and pins the sibling's `CONTRACT_VERSION`. Dependency cycles are not allowed; introduce or reshape a parent brick instead.
- Every persistent application-state resource has exactly one owning brick. Brick-local takes and ledger entries are owned implicitly by their brick.
- Bricks are used only inside this repository. Do not package or expose them for outside consumers.
- Takes and tracked ledger entries are bounded by configuration and tracked in Git. A ledger entry is tracked only for a run that went fresh, recorded or failed; every other entry, and every entry of a run with `BRICK_TEST=1`, is local and gitignored.
- Keep only a small number of high-level smoke tests, while allowing focused tests for complex `src/` logic.

## Contract placement and precedence

`AGENTS.md` rules apply to their folder and descendants. A nested `AGENTS.md` adds rules for its subtree and may not relax a parent rule.

Not every folder needs an `AGENTS.md`. Add one only where the folder introduces a distinct responsibility or additional rule. This boilerplate keeps contracts at the repository root, `bricks/`, and each brick's `input/`, `runner/`, and `src/` boundaries. A brick-level `AGENTS.md` is optional and should contain only domain-specific additions.

## When to split a brick

Split or reshape a brick when its `run` becomes a large operation dispatcher, its adapter set is no longer easy to understand, or unrelated changes repeatedly touch the same `src/`. Encapsulation is not permission for an internal monolith.
