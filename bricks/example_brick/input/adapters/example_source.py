"""Replace with an adapter for one source port: this file implements ``example_source``."""

from ..tape import once


def fetch(request: dict, run_context: dict, *, take: str = "default") -> dict:
    """Fetch data on this port's tape: replayed by default, fresh or recorded when the run asks."""
    response, _ = once(run_context, "example_source", take, request, lambda: _call(request))
    return response


def _call(request: dict) -> dict:
    """Replace with the real call to the outside source."""
    raise NotImplementedError
