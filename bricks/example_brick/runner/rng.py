# BOILERPLATE: runner.rng@1
"""Run ID creation belongs here."""

from datetime import datetime, timezone
import secrets


def create_run_id() -> str:
    """Return a new run ID: sortable by time to the microsecond, with a random tail."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    return f"{stamp}-{secrets.token_hex(2)}"
