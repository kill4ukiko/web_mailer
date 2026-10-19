"""Campaign batch delivery tasks.

send_campaign drives the whole dispatch loop:
  1. verify campaign is sending and circuit is healthy
  2. honour the delivery window (defer if closed)
  3. honour the global kill switch and the hourly / daily send caps
  4. take the campaign's fair share of the shared budget (priority-aware)
  5. claim a chunk of recipients atomically (FOR UPDATE SKIP LOCKED)
  6. deliver via Brevo, paced by the global collision-averse pacer
  7. mark recipients sent/failed and update campaign stats
  8. re-queue itself until the queue is drained

The task sends only a small chunk per invocation and re-queues, so several
campaigns interleave fairly through the single worker and the budget/priority
re-balances every cycle.
"""
from __future__ import annotations

import logging

from app.config import MAX_BATCH_SIZE, MAX_SEND_RATE_PER_MINUTE, get_settings
from app.core.budget import tick_budgets
from app.core.mailer_config import effective_base_url, effective_settings
from app.core.pacing import PacingGate
from app.core.scheduling import seconds_until_window_open
from app.workers.celery_app import celery

logger = logging.getLogger(__name__)

# Cap how long we'll sleep in a single deferred task before re-checking.
MAX_DEFER_SECONDS = 3600
# How long to wait before retrying once a rate cap has been hit.
CAP_RETRY_SECONDS = 120


@celery.task(name="app.workers.tasks_campaign.send_campaign", bind=True, max_retries=None)
def send_campaign(self, campaign_id: str) -> dict:
    """Celery entry point: run one cycle, re-queue while work remains."""
    result = _send_once(campaign_id)
    if result.get("requeue_in"):
        _requeue(campaign_id, result["requeue_in"])
    return result


def _send_once(campaign_id: str) -> dict:
    """Run a single dispatch cycle for a campaign.

    Never re-queues itself: the returned dict carries ``requeue_in`` (seconds)
    when the caller should run another cycle (Celery re-queues; the cron driver
    simply loops). A ``completed`` status means the queue is drained.
    """
    from app.core.brevo import BrevoClient
    from app.core import killswitch
    from app.core.supabase_client import get_supabase

    config = get_settings()
    supabase = get_supabase()
    campaign = supabase.get_campaign(campaign_id)

    if campaign is None:
        logger.error("campaign %s not found", campaign_id)
        return {"campaign_id": campaign_id, "status": "not_found"}

    if campaign.get("status") != "sending" or campaign.get("circuit_state") not in ("ok", "warning"):
        logger.warning("campaign %s not sendable (status=%s, circuit=%s)",
                       campaign_id, campaign.get("status"), campaign.get("circuit_state"))
        return {"campaign_id": campaign_id, "status": "skipped"}

    settings = effective_settings(supabase)

    # --- Global kill switch: stop sending entirely while paused. ---
    if settings.get("sending_paused") or killswitch.is_paused():
        return {"campaign_id": campaign_id, "status": "paused_by_killswitch",
                "requeue_in": CAP_RETRY_SECONDS}

    # --- Delivery window gate: defer if outside the allowed window. ---
    wait = seconds_until_window_open(
        campaign.get("window_start"),
        campaign.get("window_end"),
        campaign.get("window_timezone"),
    )
    if wait > 0:
        countdown = min(wait, MAX_DEFER_SECONDS)
        logger.info("campaign %s outside delivery window; retrying in %ss", campaign_id, countdown)
        return {"campaign_id": campaign_id, "status": "deferred", "requeue_in": countdown}

    # --- Fair-share budget (global caps + priority). ---
    batch_size = min(
        campaign.get("batch_size") or settings.get("default_batch_size") or 500,
        MAX_BATCH_SIZE,
    )
    budgets, retry, reason = tick_budgets(supabase, settings, batch_size)
    allowance = budgets.get(campaign_id, 0)
    if allowance <= 0:
        if reason in ("day", "hour"):
            # A global send cap is exhausted: hold the campaign until it resets
            # instead of idling invisibly in "sending".
            _hold_campaign(supabase, campaign, reason, retry)
            return {"campaign_id": campaign_id, "status": "held", "requeue_in": retry}
        # Another campaign holds high priority: sleep briefly and re-check.
        return {"campaign_id": campaign_id, "status": "deferred", "requeue_in": retry}

    effective_rate = _effective_rate(campaign, settings, config)
    chunk = min(allowance, config.max_sends_per_tick, batch_size)
    claimed = supabase.claim_recipients(campaign_id, chunk)
    if not claimed:
        # Nothing claimable -> drain leftover ineligible recipients and finish.
        _complete_campaign(supabase, campaign_id)
        return {"campaign_id": campaign_id, "status": "completed", "claimed": 0}

    brevo = BrevoClient()
    base_url = effective_base_url(supabase)
    recipients = [
        {
            "recipient_id": row["recipient_id"],
            "email": row["email"],
            "first_name": row.get("first_name"),
            "last_name": row.get("last_name"),
            "metadata": row.get("metadata") or {},
        }
        for row in claimed
    ]

    def headers_factory(recipient: dict) -> dict[str, str]:
        base = base_url.rstrip("/")
        return {
            "List-Unsubscribe": (
                f"<{base}/api/unsubscribe?cid={campaign_id}&rid={recipient['recipient_id']}>"
            ),
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            # Lets Brevo webhooks attribute opens/clicks/bounces to our rows.
            "X-Mailin-custom": (
                f"campaign={campaign_id};recipient={recipient['recipient_id']}"
            ),
        }

    def content_factory(recipient: dict) -> dict:
        from app.core.content import render_email

        context = {
            "first_name": recipient.get("first_name"),
            "last_name": recipient.get("last_name"),
            "email": recipient.get("email"),
            **(recipient.get("metadata") or {}),
        }
        html_body, text_body = render_email(
            campaign["html_content"],
            campaign.get("plain_text_content"),
            campaign_id,
            recipient["recipient_id"],
            base_url,
            context=context,
        )
        return {"html": html_body, "text": text_body}

    # Global collision-averse pacing: no two sends anywhere share a timestamp.
    gate = PacingGate(
        effective_rate,
        campaign_id,
        collision_window=settings.get("collision_window_seconds")
        or config.collision_window_seconds,
        gap_min=settings.get("cross_gap_min_seconds") or config.cross_gap_min_seconds,
        gap_max=settings.get("cross_gap_max_seconds") or config.cross_gap_max_seconds,
    )

    results = brevo.send_batch(
        recipients=recipients,
        subject=campaign["subject"],
        html_content=campaign["html_content"],
        plain_text=campaign.get("plain_text_content"),
        from_email=campaign["from_email"],
        from_name=campaign["from_name"],
        headers_factory=headers_factory,
        content_factory=content_factory,
        reply_to=settings.get("reply_to_email"),
        gate=gate,
        cancel_check=killswitch.is_paused,
    )

    sent_ids = [r.recipient_id for r in results if r.ok]
    failed_ids = [r.recipient_id for r in results if not r.ok]

    if sent_ids:
        supabase.bulk_update_recipients(sent_ids, {"status": "sent", "sent_at": "now()"})
        supabase.increment_stats(campaign_id, "sent", delta=len(sent_ids))
        email_by_recipient = {r["recipient_id"]: r["email"] for r in recipients}
        supabase.bulk_touch_contact_lifecycle(
            [email_by_recipient[rid] for rid in sent_ids if rid in email_by_recipient],
            "sent",
        )
    if failed_ids:
        supabase.bulk_update_recipients(failed_ids, {"status": "failed", "last_error": "brevo send failed"})

    # A full chunk means there is (probably) more work -> run again soon; the
    # pacer guarantees the next send still respects the rate + collisions.
    if len(claimed) >= chunk:
        requeue_in = max(1, int(60.0 / max(1, effective_rate)))
    else:
        gate.reset()
        _complete_campaign(supabase, campaign_id)
        requeue_in = None

    result = {
        "campaign_id": campaign_id,
        "status": "sending" if requeue_in else "completed",
        "claimed": len(claimed),
        "sent": len(sent_ids),
        "failed": len(failed_ids),
    }
    if requeue_in:
        result["requeue_in"] = requeue_in
    return result


def _effective_rate(campaign: dict, settings: dict, config) -> int:
    """Campaign rate, clamped so it can never exceed the global ceiling.

    The global ceiling also honours the active plan limit and any warm-up
    ramp, so a per-campaign rate can never open the throttle beyond what the
    plan (or warm-up) allows.
    """
    from app.core.mailer_config import runtime_caps

    caps = runtime_caps(settings)
    global_rate = caps["rate"] or int(
        settings.get("send_rate_per_minute") or config.default_send_rate_per_minute
    )
    rate = campaign.get("send_rate_per_minute") or global_rate
    return max(1, min(int(rate), global_rate, MAX_SEND_RATE_PER_MINUTE))


def _cap_usage(supabase, reason: str) -> int:
    """Recipients already sent inside the current cap window (best effort)."""
    from datetime import datetime, timezone

    from app.core.budget import _iso

    now = datetime.now(timezone.utc)
    if reason == "hour":
        start = now.timestamp() - (now.minute * 60 + now.second)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        return supabase.count_sent_since(_iso(start))
    except Exception:  # noqa: BLE001
        return 0


def _hold_campaign(supabase, campaign: dict, reason: str, resets_in: int) -> None:
    """Mark a campaign ``held`` until an exhausted global cap resets + alert."""
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, resets_in))).isoformat()
    supabase.update_campaign(campaign["id"], {
        "status": "held",
        "held_reason": reason,
        "held_until": until,
    })
    try:
        from app.core.alerts import cap_hold
        from app.core.mailer_config import runtime_caps

        settings = effective_settings(supabase)
        cap = runtime_caps(settings).get(reason, 0)
        cap_hold(reason, sent=_cap_usage(supabase, reason), cap=cap, resets_in=resets_in)
    except Exception as exc:  # noqa: BLE001 - alerts must never break the loop
        logger.warning("hold notification failed: %s", exc)


def _requeue(campaign_id: str, countdown: int) -> None:
    send_campaign.apply_async(args=[campaign_id], countdown=max(1, countdown))


def _complete_campaign(supabase, campaign_id: str) -> None:
    """Mark a campaign completed, skipping any leftover ineligible recipients."""
    leftovers = supabase.list_recipients(campaign_id, status="queued")
    if leftovers:
        supabase.bulk_update_recipients(
            [r["id"] for r in leftovers],
            {"status": "skipped", "last_error": "ineligible at completion (suppressed/inactive)"},
        )
    supabase.update_campaign(campaign_id, {
        "status": "completed",
        "circuit_state": "ok",
        "finished_at": "now()",
    })

    try:
        from app.core.notifications import notify_campaign

        campaign = supabase.get_campaign(campaign_id) or {}
        stats = campaign.get("stats") or {}
        skipped = len(leftovers)
        notify_campaign(campaign, "completed", [
            f"Sent: {stats.get('sent', 0)}",
            f"Opens: {stats.get('opens', 0)}",
            f"Clicks: {stats.get('clicks', 0)}",
            f"Failed: {stats.get('failed', stats.get('bounces', 0))}",
            f"Skipped: {skipped}",
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning("completion notification failed: %s", exc)


@celery.task(name="app.workers.tasks_campaign.campaign_progress_alerts")
def campaign_progress_alerts() -> dict:
    """Periodically report engagement (sent/opens/clicks) per campaign.

    Driven by Celery Beat. Covers in-flight campaigns *and* ones finished within
    the last day, since opens/clicks usually arrive after completion. Only sends
    a Telegram alert when a campaign's totals changed since the last alert
    (tracked in Redis) so a long send doesn't spam the chat with identical
    messages. Unsubscribes are alerted immediately elsewhere (tracking/webhooks).
    """
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    campaigns = supabase.list_engagement_campaigns()
    notified: list[str] = []

    try:
        from app.extensions import _RedisHolder

        redis = _RedisHolder.get()
    except Exception:  # noqa: BLE001 - notifications still work without dedupe
        redis = None

    from app.core.notifications import notify_campaign

    for campaign in campaigns:
        campaign_id = campaign["id"]
        stats = campaign.get("stats") or {}
        snapshot = (
            f"{stats.get('sent', 0)}:{stats.get('opens', 0)}:{stats.get('clicks', 0)}"
        )
        key = f"progress:{campaign_id}"
        if redis is not None:
            try:
                if redis.get(key) == snapshot:
                    continue
                redis.set(key, snapshot, ex=86400)
            except Exception:  # noqa: BLE001
                pass
        notify_campaign(campaign, "progress", [
            f"Sent: {stats.get('sent', 0)}",
            f"Opens: {stats.get('opens', 0)}",
            f"Clicks: {stats.get('clicks', 0)}",
        ])
        notified.append(campaign_id)

    return {"notified": notified}


@celery.task(name="app.workers.tasks_campaign.dispatch_scheduled")
def dispatch_scheduled() -> dict:
    """Promote due 'scheduled' campaigns to 'sending' and dispatch them.

    Driven by Celery Beat so campaigns with a future ``schedule_at`` start on
    their own. Campaigns that have no attached recipients are failed loudly
    rather than silently completing.
    """
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    dispatched, failed = _promote_due(supabase)
    for campaign_id in dispatched:
        send_campaign.delay(campaign_id)
    return {"dispatched": dispatched, "failed": failed}


def _promote_due(supabase) -> tuple[list[str], list[str]]:
    """Flip due scheduled campaigns to 'sending'. Returns (dispatched, failed).

    Shared by the Celery beat task and the cron driver; the caller is
    responsible for actually sending the promoted campaigns.
    """
    dispatched: list[str] = []
    failed: list[str] = []

    for campaign in supabase.list_due_scheduled():
        campaign_id = campaign["id"]
        if supabase.count_recipients(campaign_id) == 0:
            supabase.update_campaign(campaign_id, {
                "status": "failed",
                "finished_at": "now()",
            })
            failed.append(campaign_id)
            logger.warning("scheduled campaign %s has no recipients; marked failed", campaign_id)
            try:
                from app.core.notifications import notify_campaign

                notify_campaign(campaign, "failed", ["No recipients attached."])
            except Exception:  # noqa: BLE001
                pass
            continue

        supabase.update_campaign(campaign_id, {
            "status": "sending",
            "started_at": "now()",
        })
        dispatched.append(campaign_id)

    return dispatched, failed


@celery.task(name="app.workers.tasks_campaign.resume_sending")
def resume_sending() -> dict:
    """Re-queue any sending campaign that still has queued recipients.

    Safety net so campaigns resume automatically after a cap/hold window (hour /
    day reset, priority hand-off) even if a countdown was lost. Safe to run
    repeatedly: ``claim_recipients`` locks rows, so duplicate tasks simply find
    nothing to send.
    """
    from app.core import killswitch
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    if effective_settings(supabase).get("sending_paused") or killswitch.is_paused():
        return {"resumed": [], "skipped": "paused"}

    resumed: list[str] = []
    for campaign in supabase.list_sending_campaigns():
        if supabase.count_recipients(campaign["id"], status="queued") > 0:
            send_campaign.apply_async(args=[campaign["id"]], countdown=1)
            resumed.append(campaign["id"])
    return {"resumed": resumed}


@celery.task(name="app.workers.tasks_campaign.resume_held_campaigns")
def resume_held_campaigns() -> dict:
    """Flip cap-held campaigns back to 'sending' once their cap resets.

    Held campaigns are excluded from ``list_sending_campaigns`` (and therefore
    from the normal send loop), so this task is what brings them back after an
    hourly / daily cap boundary. Campaigns whose queue is already drained are
    completed instead of resumed.
    """
    from app.core import killswitch
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    if effective_settings(supabase).get("sending_paused") or killswitch.is_paused():
        return {"resumed": [], "skipped": "paused"}

    resumed, completed = _flip_held_to_sending(supabase)
    for campaign_id in resumed:
        send_campaign.apply_async(args=[campaign_id], countdown=1)
    return {"resumed": resumed, "completed": completed}


def _flip_held_to_sending(supabase) -> tuple[list[str], list[str]]:
    """Resume (or complete) held campaigns once the cap window has reset.

    Returns ``(resumed_ids, completed_ids)``. Shared by the Celery beat task and
    the Redis-less cron driver (which then drains the resumed campaigns itself).
    """
    from app.core.alerts import cap_resume
    from app.core.budget import remaining_global

    held = supabase.list_held_campaigns()
    if not held:
        return [], []

    headroom, retry, _reason = remaining_global(supabase, effective_settings(supabase))
    if headroom <= 0:
        return [], []

    resumed: list[str] = []
    completed: list[str] = []
    for campaign in held:
        campaign_id = campaign["id"]
        if supabase.count_recipients(campaign_id, status="queued") <= 0:
            _complete_campaign(supabase, campaign_id)
            completed.append(campaign_id)
            continue
        supabase.update_campaign(campaign_id, {
            "status": "sending",
            "held_reason": None,
            "held_until": None,
        })
        resumed.append(campaign_id)

    if resumed:
        cap_resume(held[0].get("held_reason") or "day", resumed=len(resumed),
                   resets_in=max(60, retry))
    return resumed, completed
