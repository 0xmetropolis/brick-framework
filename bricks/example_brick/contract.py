"""Typed, versioned boundary and ownership declarations for this brick."""

from typing import TypedDict


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

# True when this brick is a Live Adapter: it drives a live source over a session,
# records it through Transcript or through(), and replays by default. Only such a
# brick may receive a workflow's mode (strict lane; see README, Live Adapters).
LIVE_ADAPTER = False

# A workflow only: the Live Adapter siblings it passes its mode to. Each must be an
# orchestrated dependency that declares LIVE_ADAPTER = True, and the adapter must
# take the flags from pass_mode(). Everything else a workflow calls replays.
MODE_PASSING: tuple[str, ...] = ()

# Map sibling brick names to "eventual" or "orchestrated".
SIBLING_DEPENDENCIES: dict[str, str] = {}

# Stable identifiers for application state owned exclusively by this brick.
# Brick-local evidence under input/data and runner/runs is owned implicitly.
OWNED_STATE: tuple[str, ...] = ()


class BrickInput(TypedDict):
    """Input accepted by run(). Replace with domain fields."""


class BrickOutput(TypedDict):
    """Output returned by run(). Replace with domain fields."""
