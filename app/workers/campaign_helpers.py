"""Small helpers shared by the email and SMS worker modules.

Kept separate from the task modules so neither has to import the other (which
would create a circular import through :mod:`app.workers.celery_app`).
"""
from __future__ import annotations

# Cap how long we'll sleep in a single deferred task before re-checking.
MAX_DEFER_SECONDS = 3600


def _seconds_until(timestamp: object) -> int:
    """Seconds until an ISO timestamp (0 when unset/past/unparseable)."""
    if not timestamp:
        return 0
    from datetime import datetime, timezone

    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return 0
    if not isinstance(timestamp, datetime):
        return 0
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return max(0, int((timestamp - datetime.now(timezone.utc)).total_seconds()))
