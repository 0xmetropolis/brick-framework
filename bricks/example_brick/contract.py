"""Typed, versioned boundary and ownership declarations for this brick."""

from typing import TypedDict


CONTRACT_VERSION = 1

# The enforcement class this brick declares for itself: "strict" (the regular
# brick), "pure" (output is a function of input alone), or "workflow" (invoked
# as a whole, never depended on). A lane only adds rules. Omitting it means
# "strict".
LANE = "strict"

# True: src/ takes the clock, randomness and generated IDs as input and the
# linter holds it to that (no random, time, threading, uuid, secrets, .now()).
# False: nondeterminism is intended (a throughput or load flow); say why in a
# comment. Omit it and the brick makes no promise and gets no check.
DETERMINISTIC = True

# Map sibling brick names to "eventual" or "orchestrated".
SIBLING_DEPENDENCIES: dict[str, str] = {}

# Stable identifiers for application state owned exclusively by this brick.
# Brick-local evidence under input/data and runner/runs is owned implicitly.
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    """Input accepted by run(). Replace with domain fields."""


class BrickOutput(TypedDict):
    """Output returned by run(). Replace with domain fields."""
