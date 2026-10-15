"""Cron-driven send engine for hosts without Redis / Celery.

On a shared host (e.g. cPanel) there is no Celery worker or beat. Set
``SEND_MODE=cron`` and drive the queue with two system cron jobs::

    * * * * *  cd /path/to/app && python -m app.cron tick       # every minute
    # one-off, right after deploy (then remove):
               python -m app.cron bootstrap

``tick`` promotes due scheduled campaigns, then runs the normal send cycle for
every ``sending`` campaign within a short time budget. ``bootstrap`` applies the
SQL migrations and makes sure the settings row exists.
"""
from __future__ import annotations

import argparse
from pathlib import Path

# How long a single tick may spend delivering before returning to cron.
DEFAULT_TICK_BUDGET_SECONDS = 50.0
# Minimum gap between Telegram progress summaries (no Redis to dedupe on).
PROGRESS_ALERT_INTERVAL = 900.0


def _state_file() -> Path:
    """A small on-disk marker used to throttle periodic alerts."""
    import os
    import tempfile

    base = os.environ.get("CRON_STATE_DIR") or tempfile.gettempdir()
    path = Path(base) / "webmailer-cron.state"
    return path


def _read_last_alert() -> float:
    try:
        return float(_state_file().read_text().strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _write_last_alert(value: float) -> None:
    try:
        _state_file().write_text(str(value))
    except Exception:  # noqa: BLE001
        pass


def tick(budget_seconds: float = DEFAULT_TICK_BUDGET_SECONDS) -> dict:
    """Promote due campaigns then drain sending campaigns for a short budget."""
    import logging
    import time as _time

    from app.core import killswitch
    from app.core.supabase_client import get_supabase
    from app.workers.tasks_campaign import _flip_held_to_sending, _promote_due, _send_once

    logger = logging.getLogger("app.cron")
    supabase = get_supabase()

    dispatched, failed = _promote_due(supabase)

    cycles = 0
    completed: list[str] = []
    resumed_held: list[str] = []
    if not killswitch.is_paused():
        # Bring back any campaigns held by a since-reset hourly/daily cap.
        resumed_held, _held_completed = _flip_held_to_sending(supabase)
        deadline = _time.monotonic() + max(1.0, budget_seconds)
        while _time.monotonic() < deadline:
            sending = supabase.list_sending_campaigns()
            if not sending:
                break
            progressed = False
            for campaign in sending:
                if _time.monotonic() >= deadline:
                    break
                try:
                    result = _send_once(campaign["id"])
                except Exception as exc:  # noqa: BLE001 - one bad campaign must not kill the tick
                    logger.warning("send cycle failed for %s: %s", campaign["id"], exc)
                    continue
                cycles += 1
                status = result.get("status")
                if status == "completed":
                    completed.append(campaign["id"])
                    progressed = True
                elif status in ("sending", "deferred"):
                    progressed = True
            if not progressed:
                break

    _maybe_progress_alert()

    return {
        "dispatched": dispatched,
        "failed": failed,
        "cycles": cycles,
        "completed": completed,
        "resumed_held": resumed_held,
    }


def _maybe_progress_alert(min_interval: float = PROGRESS_ALERT_INTERVAL) -> None:
    """Send a Telegram progress summary at most every ``min_interval`` seconds."""
    import time as _time

    now = _time.time()
    if now - _read_last_alert() < min_interval:
        return
    try:
        from app.workers.tasks_campaign import campaign_progress_alerts

        campaign_progress_alerts()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _write_last_alert(now)


def bootstrap() -> dict:
    """Apply migrations and make sure the settings row exists (idempotent)."""
    from app.core.supabase_client import get_supabase
    from scripts.apply_sql import apply_migrations

    result: dict = {"migrations": "ok", "settings": "exists"}

    try:
        failures = apply_migrations(log=lambda msg: print(msg, flush=True))
        result["migrations"] = "ok" if not failures else f"warnings: {', '.join(failures)}"
    except Exception as exc:  # noqa: BLE001
        result["migrations"] = f"error: {exc}"

    try:
        from app.core.mailer_config import fallback_settings

        supabase = get_supabase()
        if supabase.get_settings_row() is None:
            supabase.update_settings(fallback_settings())
            result["settings"] = "seeded"
    except Exception as exc:  # noqa: BLE001
        result["settings"] = f"error: {exc}"

    return result


def main() -> int:
    import json

    parser = argparse.ArgumentParser(description="web_mailer cron driver")
    parser.add_argument("command", choices=["tick", "bootstrap"])
    parser.add_argument("--budget", type=float, default=DEFAULT_TICK_BUDGET_SECONDS,
                        help="seconds of sending work per tick (default 50)")
    args = parser.parse_args()

    if args.command == "bootstrap":
        result = bootstrap()
    else:
        result = tick(budget_seconds=args.budget)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
