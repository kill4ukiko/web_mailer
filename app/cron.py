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


def _state_file(name: str = "webmailer-cron.state") -> Path:
    """A small on-disk marker used to throttle periodic alerts."""
    import os
    import tempfile

    base = os.environ.get("CRON_STATE_DIR") or tempfile.gettempdir()
    path = Path(base) / name
    return path


def _read_last_alert(name: str = "webmailer-cron.state") -> float:
    try:
        return float(_state_file(name).read_text().strip() or 0)
    except Exception:  # noqa: BLE001
        return 0.0


def _write_last_alert(value: float, name: str = "webmailer-cron.state") -> None:
    try:
        _state_file(name).write_text(str(value))
    except Exception:  # noqa: BLE001
        pass


def tick(budget_seconds: float = DEFAULT_TICK_BUDGET_SECONDS) -> dict:
    """Promote and drain all channels with a fair share of the tick budget.

    Email used to consume the entire budget before SMS ran; Bird would then
    starve. Each enabled channel now gets an equal slice of the deadline.
    """
    import logging
    import time as _time

    from app.core import killswitch
    from app.core.supabase_client import get_supabase
    from app.workers.tasks_campaign import (
        _flip_held_to_sending,
        _promote_due,
        _seconds_until,
        _send_once,
    )
    from app.workers.tasks_sms import (
        _flip_held_sms_to_sending,
        _promote_due_sms,
        _send_once_sms,
    )
    from app.workers.tasks_bird_sms import (
        _promote_due as _promote_due_bird,
        _send_once as _send_once_bird,
    )

    logger = logging.getLogger("app.cron")
    supabase = get_supabase()

    dispatched, failed = _promote_due(supabase)
    sms_dispatched, sms_failed = _promote_due_sms(supabase)

    bird_store = None
    bird_dispatched: list[str] = []
    bird_failed: list[str] = []
    try:
        from app.core.bird_sms_store import get_bird_store
        bird_store = get_bird_store()
        bird_dispatched, bird_failed = _promote_due_bird(bird_store)
    except Exception as exc:  # noqa: BLE001 - pre-migration installs keep working
        logger.warning("bird promote failed: %s", exc)

    start = _time.monotonic()
    deadline = start + max(1.0, budget_seconds)
    active_channels = 1
    if not killswitch.is_sms_paused():
        active_channels += 1
    if bird_store is not None and not killswitch.is_bird_sms_paused():
        active_channels += 1
    slice_seconds = max(1.0, budget_seconds / active_channels)

    def _drain(sending_fn, send_once, seconds_until, channel_deadline):
        cycles, completed, progressed_ids = 0, [], []
        while _time.monotonic() < channel_deadline:
            sending = sending_fn()
            if not sending:
                break
            progressed = False
            for campaign in sending:
                if _time.monotonic() >= channel_deadline:
                    break
                if seconds_until(campaign.get("next_send_at")) > 0:
                    continue
                try:
                    result = send_once(campaign["id"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("send cycle failed for %s: %s", campaign["id"], exc)
                    continue
                cycles += 1
                status = result.get("status")
                if status == "completed":
                    completed.append(campaign["id"])
                    progressed = True
                elif status in ("sending", "held"):
                    progressed = True
                    progressed_ids.append(campaign["id"])
            if not progressed:
                break
        return cycles, completed, progressed_ids

    cycles = sms_cycles = bird_cycles = 0
    completed: list[str] = []
    sms_completed: list[str] = []
    bird_completed: list[str] = []
    resumed_held: list[str] = []
    sms_resumed_held: list[str] = []
    bird_resumed_held: list[str] = []

    if not killswitch.is_paused():
        resumed_held, _ = _flip_held_to_sending(supabase)
        channel_deadline = min(deadline, _time.monotonic() + slice_seconds)
        cycles, completed, _ = _drain(
            supabase.list_sending_campaigns, _send_once, _seconds_until, channel_deadline)

    if not killswitch.is_sms_paused():
        sms_resumed_held, _ = _flip_held_sms_to_sending(supabase)
        channel_deadline = min(deadline, _time.monotonic() + slice_seconds)
        sms_cycles, sms_completed, _ = _drain(
            supabase.list_sending_sms_campaigns, _send_once_sms, _seconds_until, channel_deadline)

    if bird_store is not None and not killswitch.is_bird_sms_paused():
        try:
            from app.core.bird_sms_budget import remaining_global_bird
            from app.core.bird_sms_config import effective_bird_settings
            from app.workers.tasks_bird_sms import resume_held_bird_sms_campaigns

            headroom, _retry, _reason = remaining_global_bird(
                bird_store, effective_bird_settings(bird_store))
            if headroom > 0:
                for campaign in bird_store.list_held_sms_campaigns():
                    bird_store.update_sms_campaign(campaign["id"], {
                        "status": "sending", "held_reason": None,
                        "held_until": None, "next_send_at": None,
                    })
                    bird_resumed_held.append(campaign["id"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("bird held resume failed: %s", exc)
        channel_deadline = min(deadline, _time.monotonic() + slice_seconds)
        bird_cycles, bird_completed, _ = _drain(
            bird_store.list_sending_sms_campaigns, _send_once_bird, _seconds_until, channel_deadline)

    _maybe_progress_alert()
    _maybe_sms_progress_alert()
    _maybe_bird_progress_alert()

    return {
        "dispatched": dispatched, "failed": failed, "cycles": cycles,
        "completed": completed, "resumed_held": resumed_held,
        "sms_dispatched": sms_dispatched, "sms_failed": sms_failed,
        "sms_cycles": sms_cycles, "sms_completed": sms_completed,
        "sms_resumed_held": sms_resumed_held,
        "bird_dispatched": bird_dispatched, "bird_failed": bird_failed,
        "bird_cycles": bird_cycles, "bird_completed": bird_completed,
        "bird_resumed_held": bird_resumed_held,
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


def _maybe_sms_progress_alert(min_interval: float = PROGRESS_ALERT_INTERVAL) -> None:
    """Send a Telegram SMS progress summary at most every ``min_interval``."""
    import time as _time

    name = "webmailer-cron-sms.state"
    now = _time.time()
    if now - _read_last_alert(name) < min_interval:
        return
    try:
        from app.workers.tasks_sms import sms_progress_alerts

        sms_progress_alerts()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _write_last_alert(now, name)


def _maybe_bird_progress_alert(min_interval: float = PROGRESS_ALERT_INTERVAL) -> None:
    """Send a Telegram Bird SMS progress summary at most every ``min_interval``."""
    import time as _time

    name = "webmailer-cron-bird.state"
    now = _time.time()
    if now - _read_last_alert(name) < min_interval:
        return
    try:
        from app.workers.tasks_bird_sms import bird_sms_progress_alerts

        bird_sms_progress_alerts()
    except Exception:  # noqa: BLE001
        pass
    finally:
        _write_last_alert(now, name)

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

    try:
        from app.core.sms_config import fallback_sms_settings

        supabase = get_supabase()
        if supabase.get_sms_settings_row() is None:
            supabase.update_sms_settings(fallback_sms_settings())
            result["sms_settings"] = "seeded"
        else:
            result["sms_settings"] = "exists"
    except Exception as exc:  # noqa: BLE001
        result["sms_settings"] = f"error: {exc}"

    try:
        from app.core.bird_sms_config import fallback_bird_settings
        from app.core.bird_sms_store import get_bird_store

        store = get_bird_store()
        if store.get_settings_row() is None:
            store.update_settings(fallback_bird_settings())
            result["bird_sms_settings"] = "seeded"
        else:
            result["bird_sms_settings"] = "exists"
    except Exception as exc:  # noqa: BLE001
        result["bird_sms_settings"] = f"error: {exc}"

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
