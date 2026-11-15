# The entry shape

Everything in the open shape applies. An entry brick has a `__main__.py`: it is invoked
as a whole operation and never depended on; the linter has already confined
`__main__.py` and the smoke tests to this shape and kept it out of every joint. You
judge whether the brick is still a composition and not a domain in disguise.

## What to judge

Criteria 1–10 of the open shape, then:

11. **An entry brick composes; it does not compute.** `runner/run.py` and `__main__.py`
    create the run context, call `src/`, and write the ledger entry. `src/` of an entry
    brick sequences joint and adapter calls and assembles their results; a rule, a
    validation, a checklist, a parser or a scoring function in it is a domain decision
    that belongs in a brick the entry brick calls. A diff that puts a decision, a
    calculation, a retry policy or a data transformation into the runner or the
    `__main__` — anything a focused test would want to reach — has moved domain logic
    to the one place the shape's rules do not follow it. Block. The same in the entry
    brick's `src/` is advisory: raise it with the brick it should move to.

12. **Orchestration is real.** An entry brick is where `orchestrated` joints are
    honoured: it sequences the sibling calls and compensates when a later one fails. A
    diff that adds an `orchestrated` joint and no sequencing, or that leaves a partial
    failure with no compensation and no stated reason, blocks.

13. **Smoke tests go through the entry.** A smoke test defines one explicit input,
    calls the brick's top-level `run`, and asserts on the output. A smoke test that
    imports `src/`, patches an adapter, or reaches into a sibling is a focused test in
    the wrong folder. Advisory — unless it makes a sibling run fresh other than
    through a declared `follow`, which blocks.

## Severity

Criteria **11 and 12 block** (criterion 11 only for the runner and the `__main__`; in
`src/` it is advisory); criterion 13 blocks only when it makes a sibling run fresh
outside `follow`. The open shape's severities stand for criteria 1–10.
