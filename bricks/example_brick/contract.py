"""Typed, versioned boundary and ownership declarations for this brick."""

from typing import TypedDict


# The interface version: bump it when BrickInput or BrickOutput changes meaning.
# Joints in other bricks pin the version they were written against.
CONTRACT_VERSION = 1

# The enforcement class this brick declares for itself: "strict" (the regular
# brick), "pure" (output is a function of input alone), or "workflow" (invoked
# as a whole, never depended on). A lane only adds rules. Omitting it means
# "strict".
LANE = "strict"

# Meaning: the same input and the same adapter answers give the same output.
# What an adapter returns (a game state, a model answer) is an input to src/, so a
# brick that calls a live source can still say True; the promise is about src/.
# True: src/ takes the clock, randomness and generated IDs as input and the
# linter holds it to that (no random, time, threading, uuid, secrets, .now()).
# False: nondeterminism is intended (a throughput or load flow); say why in a
# comment. Omit it and the brick makes no promise and gets no check.
DETERMINISTIC = True

# Every crossing of the brick boundary, declared once and implemented by one file
# of the same name under input/adapters/. A brick with no ports is sealed:
# PORTS = {} and no input/adapters/ or input/tapes/.
#
# A source port reaches an outside system.
#   epoch      bumped when the adapter's request or response meaning changes; takes
#              recorded at another epoch are stale
#   label      optional: what the source is (a version, a build, a protocol), merged
#              into every take's request and checked on connect; a session needs one
#   followable optional: a caller may ask this source to go fresh
# A joint port reaches a sibling brick. It is named for the sibling.
#   policy     "eventual" or "orchestrated"
#   pin        the sibling's CONTRACT_VERSION this brick was written against
#   follow     optional, orchestrated only: the sibling's followable sources that this
#              brick's fresh and record reach
PORTS: dict[str, dict] = {
    "example_source": {"kind": "source", "epoch": 1},
}

# Stable identifiers for application state owned exclusively by this brick.
# Brick-local takes under input/tapes and runner/runs are owned implicitly.
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    """Input accepted by run(). Replace with domain fields."""


class BrickOutput(TypedDict):
    """Output returned by run(). Replace with domain fields."""
