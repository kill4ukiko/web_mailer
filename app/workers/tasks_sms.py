"""SMS campaign batch delivery tasks (Celery + Redis-less cron).

Mirrors :mod:`app.workers.tasks_campaign` for the SMS channel:

  1. verify the campaign is sending and the circuit is healthy
  2. honour the delivery window + SMS kill switch
  3. honour the hourly / daily recipient caps and the daily credit ceiling
  4. take a fair share of the budget (priority-aware)
  5. claim a chunk of recipients atomically (the SQL RPC uses SKIP LOCKED)
  6. deliver via Brevo SMS, paced by the shared collision-averse pacer
  7. record segments / credits / message ids and update campaign stats
  8. re-queue itself until the queue is drained

Delivery webhooks (see :mod:`app.api.sms_webhooks`) attach the later
accepted/delivered/bounce outcomes by ``message_id``.
"""
from __future__ import annotations

import logging

from app.config import (
    MAX_CYCLE_SECONDS,
    MAX_SMS_BATCH_SIZE,
    MAX_SMS_SEND_RATE_PER_MINUTE,
    STALE_CLAIM_TIMEOUT_SECONDS,
    get_settings,
)
from app.core.pacing import PacingGate, plan_cycle
from app.core.scheduling import seconds_until_window_close, seconds_until_window_open
from app.core.sms_budget import tick_sms_budgets
from app.core.sms_config import (
    effective_sms_base_url,
    effective_sms_settings,
    runtime_sms_caps,
)
from app.workers.campaign_helpers import MAX_DEFER_SECONDS, _seconds_until
from app.workers.celery_app import celery

logger = logging.getLogger(__name__)

CAP_RETRY_SECONDS = 120


def _set_next_send_at(supabase, campaign_id: str, seconds: float | None) -> None:
    """Persist when the next paced send may run (None clears the hold-off)."""
    from datetime import datetime, timedelta, timezone

    if seconds and seconds > 0:
        until = (datetime.now(timezone.utc) + timedelta(seconds=float(seconds))).isoformat()
    else:
        until = None
    supabase.update_sms_campaign(campaign_id, {"next_send_at": until})


def _stagger_seconds(campaign: dict, settings: dict) -> int:
    """Per-SMS jitter: campaign override, then global default."""
    value = campaign.get("stagger_seconds")
    if value is None:
        value = settings.get("default_stagger_seconds")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _effective_sms_rate(campaign: dict, settings: dict) -> int:
    """Campaign rate clamped to the plan/warm-up global SMS ceiling."""
    caps = runtime_sms_caps(settings)
    global_rate = caps["rate"] or int(settings.get("send_rate_per_minute") or 30)
    rate = campaign.get("send_rate_per_minute") or global_rate
    return max(1, min(int(rate), int(global_rate), MAX_SMS_SEND_RATE_PER_MINUTE))


def _payload(campaign: dict, settings: dict) -> dict:
    """Resolve the per-campaign send options with global defaults."""
    sms_type = campaign.get("sms_type") or settings.get("default_sms_type") or "transactional"
    unicode_enabled = bool(campaign.get("unicode_enabled", settings.get("unicode_enabled")))
    include_stop = bool(campaign.get("include_stop_code")) or sms_type == "marketing"
    return {
        "sender": campaign.get("sender") or settings.get("sms_sender") or "",
        "sms_type": sms_type,
        "unicode_enabled": unicode_enabled,
        "include_stop": include_stop,
        "organisation_prefix": campaign.get("organisation_prefix")
        or settings.get("organisation_prefix"),
    }


def _bump_sms_stats(supabase, campaign_id: str, **deltas: int) -> None:
    """Add ``deltas`` to the campaign's JSON stats in one read-modify-write."""
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        return
    stats = dict(campaign.get("stats") or {})
    changed = False
    for field, delta in deltas.items():
        if delta:
            stats[field] = stats.get(field, 0) + int(delta)
            changed = True
    if changed:
        supabase.update_sms_campaign(campaign_id, {"stats": stats})


@celery.task(name="app.workers.tasks_sms.send_sms_campaign", bind=True, max_retries=None)
def send_sms_campaign(self, campaign_id: str) -> dict:
    """Celery entry point: run one cycle, re-queue while work remains."""
    result = _send_once_sms(campaign_id)
    if result.get("requeue_in"):
        _requeue(campaign_id, result["requeue_in"])
    return result


def _send_once_sms(campaign_id: str) -> dict:
    """Run a single dispatch cycle for an SMS campaign.

    Never re-queues itself: the returned dict carries ``requeue_in`` seconds when
    another cycle is needed (Celery re-queues, the cron driver loops).
    """
    from app.core import killswitch
    from app.core.sms import SmsClient, build_webhook_url
    from app.core.supabase_client import get_supabase

    config = get_settings()
    supabase = get_supabase()
    campaign = supabase.get_sms_campaign(campaign_id)

    if campaign is None:
        logger.error("sms campaign %s not found", campaign_id)
        return {"campaign_id": campaign_id, "status": "not_found"}

    if campaign.get("status") != "sending" or campaign.get("circuit_state") not in ("ok", "warning"):
        logger.warning("sms campaign %s not sendable (status=%s, circuit=%s)",
                       campaign_id, campaign.get("status"), campaign.get("circuit_state"))
        return {"campaign_id": campaign_id, "status": "skipped"}

    settings = effective_sms_settings(supabase)

    # --- Kill switch: SMS has its own pause flag (separate from email). ---
    if settings.get("sending_paused") or killswitch.is_sms_paused():
        return {"campaign_id": campaign_id, "status": "paused_by_killswitch",
                "requeue_in": CAP_RETRY_SECONDS}

    # --- Pacing hold-off deferred by the previous cycle. ---
    paced_wait = _seconds_until(campaign.get("next_send_at"))
    if paced_wait > 0:
        return {"campaign_id": campaign_id, "status": "deferred",
                "requeue_in": min(paced_wait, MAX_DEFER_SECONDS)}

    # --- Delivery window gate. ---
    wait = seconds_until_window_open(
        campaign.get("window_start"),
        campaign.get("window_end"),
        campaign.get("window_timezone"),
    )
    if wait > 0:
        countdown = min(wait, MAX_DEFER_SECONDS)
        logger.info("sms campaign %s outside delivery window; retrying in %ss",
                    campaign_id, countdown)
        return {"campaign_id": campaign_id, "status": "deferred", "requeue_in": countdown}

    window_remaining = seconds_until_window_close(
        campaign.get("window_start"),
        campaign.get("window_end"),
        campaign.get("window_timezone"),
    )

    # --- Fair-share budget (caps + credits + priority). ---
    batch_size = min(
        campaign.get("batch_size") or settings.get("default_batch_size") or 500,
        MAX_SMS_BATCH_SIZE,
    )
    budgets, retry, reason = tick_sms_budgets(supabase, settings, batch_size)
    allowance = budgets.get(campaign_id, 0)
    if allowance <= 0:
        if reason in ("day", "hour", "credits"):
            _hold_sms_campaign(supabase, campaign, reason, retry)
            return {"campaign_id": campaign_id, "status": "held", "requeue_in": retry}
        return {"campaign_id": campaign_id, "status": "deferred", "requeue_in": retry}

    rate = _effective_sms_rate(campaign, settings)
    plan = plan_cycle(
        rate=rate,
        jitter=_stagger_seconds(campaign, settings),
        allowance=allowance,
        batch_size=batch_size,
        max_sends_per_tick=config.max_sends_per_tick,
        cycle_budget=MAX_CYCLE_SECONDS,
        window_remaining=window_remaining,
        stale_limit=STALE_CLAIM_TIMEOUT_SECONDS,
    )
    if plan.defer:
        next_open = seconds_until_window_open(
            campaign.get("window_start"),
            campaign.get("window_end"),
            campaign.get("window_timezone"),
        )
        countdown = min(max(next_open, CAP_RETRY_SECONDS), MAX_DEFER_SECONDS)
        return {"campaign_id": campaign_id, "status": "deferred", "requeue_in": countdown}

    chunk = plan.chunk
    claimed = supabase.claim_sms_recipients(campaign_id, chunk)
    if not claimed:
        _complete_sms_campaign(supabase, campaign_id)
        return {"campaign_id": campaign_id, "status": "completed", "claimed": 0}

    options = _payload(campaign, settings)
    recipients = [
        {
            "recipient_id": row["recipient_id"],
            "phone": row["phone"],
            "first_name": row.get("first_name"),
            "last_name": row.get("last_name"),
            "country": row.get("country"),
            "metadata": row.get("metadata") or {},
        }
        for row in claimed
    ]

    from app.core.sms_content import render_sms

    signature = (campaign.get("content") or "")
    segments_by_recipient: dict[str, int] = {}

    def content_factory(recipient: dict) -> dict:
        context = {
            "first_name": recipient.get("first_name"),
            "last_name": recipient.get("last_name"),
            "phone": recipient.get("phone"),
            "country": recipient.get("country"),
            **(recipient.get("metadata") or {}),
        }
        body, segments = render_sms(
            signature,
            context,
            include_stop=options["include_stop"],
            unicode_enabled=True if options["unicode_enabled"] else None,
        )
        segments_by_recipient[recipient["recipient_id"]] = segments
        return {"content": body, "unicode": options["unicode_enabled"]}

    base_url = effective_sms_base_url(supabase)
    web_url = build_webhook_url(base_url, config.brevo_webhook_secret)

    gate = PacingGate(
        rate,
        campaign_id,
        collision_window=settings.get("collision_window_seconds")
        or config.collision_window_seconds,
        gap_min=settings.get("cross_gap_min_seconds") or config.cross_gap_min_seconds,
        gap_max=settings.get("cross_gap_max_seconds") or config.cross_gap_max_seconds,
    )

    results = SmsClient().send_batch_sms(
        recipients=recipients,
        sender=options["sender"],
        content=signature,
        sms_type=options["sms_type"],
        tag=campaign_id,
        unicode_enabled=options["unicode_enabled"],
        organisation_prefix=options["organisation_prefix"],
        content_factory=content_factory,
        web_url_factory=lambda _recipient: web_url,
        send_rate_per_minute=rate,
        global_rate_per_minute=rate,
        jitter_ms=(0, int(plan.in_batch_jitter * 1000)),
        gate=gate,
        cancel_check=killswitch.is_sms_paused,
    )

    sent = 0
    segments_total = 0
    credits_total = 0
    sent_phones: list[str] = []
    lowest_remaining: int | None = None

    phone_by_recipient = {r["recipient_id"]: r["phone"] for r in recipients}
    for res in results:
        if res.ok:
            fallback_segments = segments_by_recipient.get(res.recipient_id or "", 1)
            segments = int(res.segments if res.segments is not None else fallback_segments)
            credits = res.credits_used if res.credits_used is not None else segments
            supabase.update_sms_recipient(res.recipient_id, {
                "status": "sent",
                "sent_at": "now()",
                "message_id": res.message_id,
                "segments": segments,
                "credits_used": credits,
                "last_error": None,
            })
            sent += 1
            segments_total += segments
            credits_total += int(credits)
            phone = phone_by_recipient.get(res.recipient_id or "")
            if phone:
                sent_phones.append(phone)
            if res.remaining_credits is not None:
                if lowest_remaining is None or res.remaining_credits < lowest_remaining:
                    lowest_remaining = res.remaining_credits
        else:
            supabase.update_sms_recipient(res.recipient_id, {
                "status": "failed",
                "last_error": (res.error or "brevo sms send failed")[:500],
            })

    failed = len(results) - sent

    if sent:
        _bump_sms_stats(
            supabase, campaign_id,
            sent=sent, segments=segments_total, credits_used=credits_total,
        )
        supabase.bulk_touch_sms_contact_lifecycle(sent_phones, "sent")

    if failed:
        _bump_sms_stats(supabase, campaign_id, failed=failed)

    if lowest_remaining is not None:
        _maybe_low_credit_alert(settings, lowest_remaining)

    if len(claimed) >= chunk:
        _set_next_send_at(supabase, campaign_id, plan.next_wait)
        requeue_in = max(1, min(MAX_DEFER_SECONDS, int(round(plan.next_wait))))
    else:
        gate.reset()
        _complete_sms_campaign(supabase, campaign_id)
        requeue_in = None

    result = {
        "campaign_id": campaign_id,
        "status": "sending" if requeue_in else "completed",
        "claimed": len(claimed),
        "sent": sent,
        "failed": failed,
        "segments": segments_total,
        "credits": credits_total,
    }
    if requeue_in:
        result["requeue_in"] = requeue_in
    return result


def _maybe_low_credit_alert(settings: dict, remaining: int) -> None:
    """Warn once per day when the Brevo SMS balance drops below the threshold."""
    try:
        threshold = int(settings.get("low_credit_threshold") or 0)
        if threshold <= 0 or remaining > threshold:
            return
        from app.core.alerts import _claim_once
        from app.core.notifications import notify

        if not _claim_once("sms-low-credits", 86400):
            return
        notify(
            "SMS credits running low",
            [f"Remaining credits: {remaining}", f"Threshold: {threshold}"],
            "warning",
        )
    except Exception as exc:  # noqa: BLE001 - alerts must never break the loop
        logger.warning("sms low-credit alert failed: %s", exc)


def _cap_usage(supabase, reason: str) -> int:
    """Usage already recorded inside the current cap window (best effort)."""
    from datetime import datetime, timezone

    from app.core.sms_budget import _iso

    now = datetime.now(timezone.utc)
    if reason == "hour":
        start = now.timestamp() - (now.minute * 60 + now.second)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        if reason == "credits":
            return supabase.count_sms_credits_since(_iso(start))
        return supabase.count_sms_sent_since(_iso(start))
    except Exception:  # noqa: BLE001
        return 0


def _hold_sms_campaign(supabase, campaign: dict, reason: str, resets_in: int) -> None:
    """Mark an SMS campaign ``held`` until an exhausted cap resets + alert."""
    from datetime import datetime, timedelta, timezone

    until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, resets_in))).isoformat()
    supabase.update_sms_campaign(campaign["id"], {
        "status": "held",
        "held_reason": reason,
        "held_until": until,
        "next_send_at": None,
    })
    try:
        from app.core.alerts import cap_hold
        from app.core.sms_config import runtime_sms_caps

        settings = effective_sms_settings(supabase)
        cap = runtime_sms_caps(settings).get(reason, 0)
        cap_hold(reason, sent=_cap_usage(supabase, reason), cap=cap,
                 resets_in=resets_in)
    except Exception as exc:  # noqa: BLE001 - alerts must never break the loop
        logger.warning("sms hold notification failed: %s", exc)


def _requeue(campaign_id: str, countdown: int) -> None:
    send_sms_campaign.apply_async(args=[campaign_id], countdown=max(1, countdown))


def _complete_sms_campaign(supabase, campaign_id: str) -> None:
    """Mark an SMS campaign completed, skipping ineligible leftovers."""
    leftovers = supabase.list_sms_recipients(campaign_id, status="queued")
    if leftovers:
        supabase.bulk_update_sms_recipients(
            [r["id"] for r in leftovers],
            {"status": "skipped",
             "last_error": "ineligible at completion (suppressed/inactive)"},
        )
        _bump_sms_stats(supabase, campaign_id, skipped=len(leftovers))
    supabase.update_sms_campaign(campaign_id, {
        "status": "completed",
        "circuit_state": "ok",
        "finished_at": "now()",
        "next_send_at": None,
    })

    try:
        from app.core.notifications import notify_sms_campaign

        campaign = supabase.get_sms_campaign(campaign_id) or {}
        stats = campaign.get("stats") or {}
        notify_sms_campaign(campaign, "completed", [
            f"Sent: {stats.get('sent', 0)}",
            f"Delivered: {stats.get('delivered', 0)}",
            f"Replied: {stats.get('replied', 0)}",
            f"Bounces: {stats.get('bounces', 0)}",
            f"Failed: {stats.get('failed', 0)}",
            f"Skipped: {len(leftovers)}",
            f"Credits: {stats.get('credits_used', 0)}",
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning("sms completion notification failed: %s", exc)


@celery.task(name="app.workers.tasks_sms.sms_progress_alerts")
def sms_progress_alerts() -> dict:
    """Report SMS progress per in-flight/recent campaign (deduped on totals)."""
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    campaigns = supabase.list_engagement_sms_campaigns()
    notified: list[str] = []

    try:
        from app.extensions import _RedisHolder

        redis = _RedisHolder.get()
    except Exception:  # noqa: BLE001
        redis = None

    from app.core.notifications import notify_sms_campaign

    for campaign in campaigns:
        campaign_id = campaign["id"]
        stats = campaign.get("stats") or {}
        snapshot = (
            f"{stats.get('sent', 0)}:{stats.get('delivered', 0)}:"
            f"{stats.get('replied', 0)}:{stats.get('bounces', 0)}"
        )
        key = f"sms-progress:{campaign_id}"
        if redis is not None:
            try:
                if redis.get(key) == snapshot:
                    continue
                redis.set(key, snapshot, ex=86400)
            except Exception:  # noqa: BLE001
                pass
        notify_sms_campaign(campaign, "progress", [
            f"Sent: {stats.get('sent', 0)}",
            f"Delivered: {stats.get('delivered', 0)}",
            f"Replied: {stats.get('replied', 0)}",
            f"Bounces: {stats.get('bounces', 0)}",
            f"Credits: {stats.get('credits_used', 0)}",
        ])
        notified.append(campaign_id)

    return {"notified": notified}


@celery.task(name="app.workers.tasks_sms.dispatch_scheduled_sms")
def dispatch_scheduled_sms() -> dict:
    """Promote due scheduled SMS campaigns to 'sending' and dispatch them."""
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    dispatched, failed = _promote_due_sms(supabase)
    for campaign_id in dispatched:
        send_sms_campaign.delay(campaign_id)
    return {"dispatched": dispatched, "failed": failed}


def _promote_due_sms(supabase) -> tuple[list[str], list[str]]:
    """Flip due scheduled SMS campaigns to 'sending'. Returns (dispatched, failed)."""
    dispatched: list[str] = []
    failed: list[str] = []

    for campaign in supabase.list_due_scheduled_sms():
        campaign_id = campaign["id"]
        if supabase.count_sms_recipients(campaign_id) == 0:
            supabase.update_sms_campaign(campaign_id, {
                "status": "failed",
                "finished_at": "now()",
            })
            failed.append(campaign_id)
            logger.warning("scheduled sms campaign %s has no recipients; marked failed",
                           campaign_id)
            try:
                from app.core.notifications import notify_sms_campaign

                notify_sms_campaign(campaign, "failed", ["No recipients attached."])
            except Exception:  # noqa: BLE001
                pass
            continue

        supabase.update_sms_campaign(campaign_id, {
            "status": "sending",
            "started_at": "now()",
            "next_send_at": None,
        })
        dispatched.append(campaign_id)

    return dispatched, failed


@celery.task(name="app.workers.tasks_sms.resume_sending_sms")
def resume_sending_sms() -> dict:
    """Re-queue sending SMS campaigns that still have queued recipients."""
    from app.core import killswitch
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    if effective_sms_settings(supabase).get("sending_paused") or killswitch.is_sms_paused():
        return {"resumed": [], "skipped": "paused"}

    resumed: list[str] = []
    for campaign in supabase.list_sending_sms_campaigns():
        if _seconds_until(campaign.get("next_send_at")) > 0:
            continue
        if supabase.count_sms_recipients(campaign["id"], status="queued") > 0:
            send_sms_campaign.apply_async(args=[campaign["id"]], countdown=1)
            resumed.append(campaign["id"])
    return {"resumed": resumed}


@celery.task(name="app.workers.tasks_sms.resume_held_sms_campaigns")
def resume_held_sms_campaigns() -> dict:
    """Flip cap-held SMS campaigns back to 'sending' once their cap resets."""
    from app.core import killswitch
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    if effective_sms_settings(supabase).get("sending_paused") or killswitch.is_sms_paused():
        return {"resumed": [], "skipped": "paused"}

    resumed, completed = _flip_held_sms_to_sending(supabase)
    for campaign_id in resumed:
        send_sms_campaign.apply_async(args=[campaign_id], countdown=1)
    return {"resumed": resumed, "completed": completed}


def _flip_held_sms_to_sending(supabase) -> tuple[list[str], list[str]]:
    """Resume (or complete) held SMS campaigns once the cap window has reset."""
    from app.core.alerts import cap_resume
    from app.core.sms_budget import remaining_global_sms

    held = supabase.list_held_sms_campaigns()
    if not held:
        return [], []

    headroom, retry, _reason = remaining_global_sms(
        supabase, effective_sms_settings(supabase)
    )
    if headroom <= 0:
        return [], []

    resumed: list[str] = []
    completed: list[str] = []
    for campaign in held:
        campaign_id = campaign["id"]
        if supabase.count_sms_recipients(campaign_id, status="queued") <= 0:
            _complete_sms_campaign(supabase, campaign_id)
            completed.append(campaign_id)
            continue
        supabase.update_sms_campaign(campaign_id, {
            "status": "sending",
            "held_reason": None,
            "held_until": None,
            "next_send_at": None,
        })
        resumed.append(campaign_id)

    if resumed:
        cap_resume(held[0].get("held_reason") or "day", resumed=len(resumed),
                   resets_in=max(60, retry))
    return resumed, completed
