# The sealed shape

A brick with no ports. Everything in the open shape applies where it can (a sealed
brick has no sources and no joints, so criteria 2, 3, 4 and 7 have nothing to judge),
and the linter has already removed adapters, tapes, direct I/O, `random`, `time` and
clock calls from this brick. What remains is the claim the shape makes and a parser
cannot verify: **the output is a function of the input alone.**

## What to judge

Criteria 1, 5, 6, 8, 9 and 10 of the open shape, then:

11. **Hidden inputs.** Anything `src/` reads that did not arrive in the brick input is
    a second input the contract does not declare: a module-level mutable, a class
    attribute mutated across calls, a field of the run context other than the input,
    a config value read inside `src/`, a default argument evaluated once. A diff that
    introduces one blocks.

12. **Semantic nondeterminism.** Iteration over a set or over dict keys derived from
    hashed objects, float accumulation whose order the output depends on, `id()`-based
    ordering, thread timing. If two calls with the same input can return different
    outputs, the brick is not sealed. Block when the output depends on it; advisory
    when only an internal order does.

13. **Sealing by relocation.** A diff that keeps `src/` deterministic by moving the
    effect into `runner/` — a runner that reads a file, calls a service, or seeds the
    input from the environment before calling `src/` — has hidden the effect where the
    shape does not look. The runner writes ledger entries and owns the RNG; it is not
    a second `input/`. Block.

## Severity

Criteria **11 and 13 block**; criterion 12 blocks when the output depends on it. The
open shape's severities stand for criteria 1, 5, 6, 8, 9 and 10.
