"""Bird SMS batch delivery tasks (Celery + Redis-less cron)."""
from __future__ import annotations

import logging

from app.config import (
    MAX_CYCLE_SECONDS,
    MAX_BIRD_BATCH_SIZE,
    MAX_BIRD_SEND_RATE_PER_MINUTE,
    STALE_CLAIM_TIMEOUT_SECONDS,
    get_settings,
)
from app.core.pacing import PacingGate, plan_cycle
from app.core.scheduling import seconds_until_window_close, seconds_until_window_open
from app.core.bird_sms_budget import tick_bird_budgets
from app.core.bird_sms_config import (
    effective_bird_base_url,
    effective_bird_settings,
    runtime_bird_caps,
)
from app.core.bird_sms_content import render_bird_sms
from app.workers.campaign_helpers import MAX_DEFER_SECONDS, _seconds_until
from app.workers.celery_app import celery

logger = logging.getLogger(__name__)
CAP_RETRY_SECONDS = 120


def _set_next_send_at(supabase, campaign_id: str, seconds: float | None) -> None:
    from datetime import datetime, timedelta, timezone
    until = ((datetime.now(timezone.utc) + timedelta(seconds=float(seconds))).isoformat()
             if seconds and seconds > 0 else None)
    supabase.update_sms_campaign(campaign_id, {"next_send_at": until})


def _stagger_seconds(campaign: dict, settings: dict) -> int:
    value = campaign.get("stagger_seconds")
    if value is None:
        value = settings.get("default_stagger_seconds")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _effective_rate(campaign: dict, settings: dict) -> int:
    caps = runtime_bird_caps(settings)
    global_rate = caps["rate"] or int(settings.get("send_rate_per_minute") or 30)
    rate = campaign.get("send_rate_per_minute") or global_rate
    return max(1, min(int(rate), int(global_rate), MAX_BIRD_SEND_RATE_PER_MINUTE))


def _bump(supabase, campaign_id: str, **deltas) -> None:
    campaign = supabase.get_sms_campaign(campaign_id)
    if not campaign:
        return
    stats = dict(campaign.get("stats") or {})
    for field, delta in deltas.items():
        if delta:
            stats[field] = stats.get(field, 0) + delta
    supabase.update_sms_campaign(campaign_id, {"stats": stats})


@celery.task(name="app.workers.tasks_bird_sms.send_bird_sms_campaign", bind=True, max_retries=None)
def send_bird_sms_campaign(self, campaign_id: str) -> dict:
    result = _send_once(campaign_id)
    if result.get("requeue_in"):
        send_bird_sms_campaign.apply_async(args=[campaign_id], countdown=result["requeue_in"])
    return result


def _send_once(campaign_id: str) -> dict:
    from app.core import killswitch
    from app.core.bird_sms import BirdSmsClient
    from app.core.bird_sms_store import get_bird_store

    config = get_settings()
    supabase = get_bird_store()
    campaign = supabase.get_sms_campaign(campaign_id)
    if campaign is None:
        logger.error("bird sms campaign %s not found", campaign_id)
        return {"campaign_id": campaign_id, "status": "not_found"}
    if campaign.get("status") != "sending" or campaign.get("circuit_state") not in ("ok", "warning"):
        return {"campaign_id": campaign_id, "status": "skipped"}

    settings = effective_bird_settings(supabase)
    if settings.get("sending_paused") or killswitch.is_bird_sms_paused():
        return {"campaign_id": campaign_id, "status": "paused_by_killswitch",
                "requeue_in": CAP_RETRY_SECONDS}

    paced_wait = _seconds_until(campaign.get("next_send_at"))
    if paced_wait > 0:
        return {"campaign_id": campaign_id, "status": "deferred",
                "requeue_in": min(paced_wait, MAX_DEFER_SECONDS)}

    wait = seconds_until_window_open(
        campaign.get("window_start"), campaign.get("window_end"),
        campaign.get("window_timezone"))
    if wait > 0:
        return {"campaign_id": campaign_id, "status": "deferred",
                "requeue_in": min(wait, MAX_DEFER_SECONDS)}
    window_remaining = seconds_until_window_close(
        campaign.get("window_start"), campaign.get("window_end"),
        campaign.get("window_timezone"))

    batch_size = min(campaign.get("batch_size") or settings.get("default_batch_size") or 250,
                     MAX_BIRD_BATCH_SIZE)
    budgets, retry, reason = tick_bird_budgets(supabase, settings, batch_size)
    allowance = budgets.get(campaign_id, 0)
    if allowance <= 0:
        if reason in ("day", "hour", "budget"):
            _hold(supabase, campaign, reason, retry)
            return {"campaign_id": campaign_id, "status": "held", "requeue_in": retry}
        return {"campaign_id": campaign_id, "status": "deferred", "requeue_in": retry}

    rate = _effective_rate(campaign, settings)
    plan = plan_cycle(
        rate=rate, jitter=_stagger_seconds(campaign, settings), allowance=allowance,
        batch_size=batch_size, max_sends_per_tick=config.max_sends_per_tick,
        cycle_budget=MAX_CYCLE_SECONDS, window_remaining=window_remaining,
        stale_limit=STALE_CLAIM_TIMEOUT_SECONDS,
    )
    if plan.defer:
        next_open = seconds_until_window_open(
            campaign.get("window_start"), campaign.get("window_end"),
            campaign.get("window_timezone"))
        return {"campaign_id": campaign_id, "status": "deferred",
                "requeue_in": min(max(next_open, CAP_RETRY_SECONDS), MAX_DEFER_SECONDS)}

    chunk = plan.chunk
    claimed = supabase.claim_sms_recipients(campaign_id, chunk)
    if not claimed:
        _complete(supabase, campaign_id)
        return {"campaign_id": campaign_id, "status": "completed", "claimed": 0}

    client = BirdSmsClient(
        api_key=settings.get("bird_api_key") or None,
        region=settings.get("bird_region") or None,
    )
    gate = PacingGate(
        rate, campaign_id,
        collision_window=settings.get("collision_window_seconds") or config.collision_window_seconds,
        gap_min=settings.get("cross_gap_min_seconds") or config.cross_gap_min_seconds,
        gap_max=settings.get("cross_gap_max_seconds") or config.cross_gap_max_seconds,
    )

    mode = campaign.get("message_mode") or "text"
    from_number = campaign.get("from_number") or settings.get("from_number")
    category = campaign.get("category") or settings.get("default_category") or "transactional"
    smart = bool(campaign.get("smart_encoding", settings.get("smart_encoding", True)))
    include_stop = bool(campaign.get("include_stop_code")) or category == "marketing"
    stop_text = "\nReply STOP to opt out." if include_stop else ""
    base_cost = float(settings.get("cost_per_segment") or 0.01)

    sent = failed = segments_total = 0
    cost_total = 0.0
    sent_phones: list[str] = []

    for recipient in claimed:
        if gate is not None:
            gate.wait()
        if killswitch.is_bird_sms_paused():
            break
        context = {
            "first_name": recipient.get("first_name"), "last_name": recipient.get("last_name"),
            "phone": recipient.get("phone"), "country": recipient.get("country"),
            **(recipient.get("metadata") or {}),
        }
        try:
            if mode == "template":
                params = campaign.get("template_parameters") or {}
                resolved = {
                    k: render_bird_sms(str(v), context)[0] if isinstance(v, str) else v
                    for k, v in params.items()
                }
                outcome = client.send_message(
                    recipient["phone"], from_number=from_number,
                    template={
                        "slug": campaign.get("template_slug"),
                        "language": campaign.get("template_language") or "en",
                        "scope": campaign.get("template_scope"),
                        "parameters": resolved,
                    },
                    smart_encoding=smart,
                )
                segments = 1
            else:
                body, segments = render_bird_sms(
                    campaign.get("text") or "", context,
                    include_stop=include_stop, smart_encoding=smart)
                if stop_text and not body.endswith(stop_text.strip()):
                    body = body + stop_text
                    segments = max(1, len(body) // 153 + 1)
                outcome = client.send_message(
                    recipient["phone"], from_number=from_number, text=body,
                    category=category, smart_encoding=smart,
                )
            cost = round(segments * base_cost, 6)
            supabase.update_sms_recipient(recipient["recipient_id"], {
                "status": "accepted", "sent_at": "now()", "message_id": outcome.message_id,
                "segments": segments, "cost": cost, "last_error": None,
            })
            sent += 1
            segments_total += segments
            cost_total += cost
            sent_phones.append(recipient["phone"])
        except Exception as exc:  # noqa: BLE001 - per-recipient failure isolation
            failed += 1
            status_code = getattr(exc, "status", None)
            code = getattr(exc, "code", None)
            supabase.update_sms_recipient(recipient["recipient_id"], {
                "status": "failed" if status_code not in (402,) else "queued",
                "last_error": str(exc)[:500],
                "error_code": code,
                "error_description": str(exc)[:500],
            })
            if status_code == 402:
                _hold(supabase, campaign, "budget", 3600)
                break

    gate.reset()
    if sent:
        _bump(supabase, campaign_id, sent=sent, accepted=sent,
              segments=segments_total, cost=round(cost_total, 6))
        supabase.bulk_touch_sms_contact_lifecycle(sent_phones, "accepted")
    if failed:
        _bump(supabase, campaign_id, failed=failed)

    if len(claimed) >= chunk:
        _set_next_send_at(supabase, campaign_id, plan.next_wait)
        requeue_in = max(1, min(MAX_DEFER_SECONDS, int(round(plan.next_wait))))
        return {"campaign_id": campaign_id, "status": "sending", "claimed": len(claimed),
                "sent": sent, "failed": failed, "segments": segments_total,
                "cost": round(cost_total, 6), "requeue_in": requeue_in}

    _complete(supabase, campaign_id)
    return {"campaign_id": campaign_id, "status": "completed", "claimed": len(claimed),
            "sent": sent, "failed": failed, "segments": segments_total,
            "cost": round(cost_total, 6)}


def _hold(supabase, campaign: dict, reason: str, resets_in: int) -> None:
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(seconds=max(1, resets_in))).isoformat()
    supabase.update_sms_campaign(campaign["id"], {
        "status": "held", "held_reason": reason, "held_until": until, "next_send_at": None,
    })
    try:
        from app.core.alerts import cap_hold
        from app.core.bird_sms_config import runtime_bird_caps

        settings = effective_bird_settings(supabase)
        cap = runtime_bird_caps(settings).get("cost" if reason == "budget" else reason, 0)
        cap_hold(reason, sent=0, cap=cap, resets_in=resets_in)
    except Exception as exc:  # noqa: BLE001
        logger.warning("bird sms hold notification failed: %s", exc)


def _complete(supabase, campaign_id: str) -> None:
    leftovers = supabase.list_sms_recipients(campaign_id, status="queued")
    if leftovers:
        supabase.bulk_update_sms_recipients(
            [r["id"] for r in leftovers],
            {"status": "skipped", "last_error": "ineligible at completion"})
        _bump(supabase, campaign_id, skipped=len(leftovers))
    supabase.update_sms_campaign(campaign_id, {
        "status": "completed", "circuit_state": "ok", "finished_at": "now()",
        "next_send_at": None,
    })
    try:
        from app.core.notifications import notify_bird_sms_campaign
        campaign = supabase.get_sms_campaign(campaign_id) or {}
        stats = campaign.get("stats") or {}
        notify_bird_sms_campaign(campaign, "completed", [
            f"Sent: {stats.get('sent', 0)}", f"Delivered: {stats.get('delivered', 0)}",
            f"Failed: {stats.get('failed', 0)}", f"Cost: {stats.get('cost', 0)}",
            f"Skipped: {len(leftovers)}",
        ])
    except Exception as exc:  # noqa: BLE001
        logger.warning("bird sms completion notification failed: %s", exc)


@celery.task(name="app.workers.tasks_bird_sms.bird_sms_progress_alerts")
def bird_sms_progress_alerts() -> dict:
    from app.core.bird_sms_store import get_bird_store
    from app.core.notifications import notify_bird_sms_campaign

    supabase = get_bird_store()
    notified = []
    try:
        from app.extensions import _RedisHolder
        redis = _RedisHolder.get()
    except Exception:  # noqa: BLE001
        redis = None
    for campaign in supabase.list_engagement_sms_campaigns():
        stats = campaign.get("stats") or {}
        snapshot = f"{stats.get('sent',0)}:{stats.get('delivered',0)}:{stats.get('failed',0)}"
        key = f"bird-sms-progress:{campaign['id']}"
        if redis is not None:
            try:
                if redis.get(key) == snapshot:
                    continue
                redis.set(key, snapshot, ex=86400)
            except Exception:  # noqa: BLE001
                pass
        notify_bird_sms_campaign(campaign, "progress", [
            f"Sent: {stats.get('sent', 0)}", f"Delivered: {stats.get('delivered', 0)}",
            f"Failed: {stats.get('failed', 0)}", f"Cost: {stats.get('cost', 0)}",
        ])
        notified.append(campaign["id"])
    return {"notified": notified}


@celery.task(name="app.workers.tasks_bird_sms.dispatch_scheduled_bird_sms")
def dispatch_scheduled_bird_sms() -> dict:
    from app.core.bird_sms_store import get_bird_store

    supabase = get_bird_store()
    dispatched, failed = _promote_due(supabase)
    for campaign_id in dispatched:
        send_bird_sms_campaign.delay(campaign_id)
    return {"dispatched": dispatched, "failed": failed}


def _promote_due(supabase) -> tuple[list[str], list[str]]:
    dispatched, failed = [], []
    for campaign in supabase.list_due_scheduled_sms():
        campaign_id = campaign["id"]
        if supabase.count_sms_recipients(campaign_id) == 0:
            supabase.update_sms_campaign(campaign_id, {"status": "failed", "finished_at": "now()"})
            failed.append(campaign_id)
            continue
        supabase.update_sms_campaign(campaign_id, {
            "status": "sending", "started_at": "now()", "next_send_at": None,
        })
        dispatched.append(campaign_id)
    return dispatched, failed


@celery.task(name="app.workers.tasks_bird_sms.resume_sending_bird_sms")
def resume_sending_bird_sms() -> dict:
    from app.core import killswitch
    from app.core.bird_sms_store import get_bird_store

    supabase = get_bird_store()
    if effective_bird_settings(supabase).get("sending_paused") or killswitch.is_bird_sms_paused():
        return {"resumed": [], "skipped": "paused"}
    resumed = []
    for campaign in supabase.list_sending_sms_campaigns():
        if _seconds_until(campaign.get("next_send_at")) > 0:
            continue
        if supabase.count_sms_recipients(campaign["id"], status="queued") > 0:
            send_bird_sms_campaign.apply_async(args=[campaign["id"]], countdown=1)
            resumed.append(campaign["id"])
    return {"resumed": resumed}


@celery.task(name="app.workers.tasks_bird_sms.resume_held_bird_sms_campaigns")
def resume_held_bird_sms_campaigns() -> dict:
    from app.core import killswitch
    from app.core.bird_sms_budget import remaining_global_bird
    from app.core.bird_sms_store import get_bird_store

    supabase = get_bird_store()
    if effective_bird_settings(supabase).get("sending_paused") or killswitch.is_bird_sms_paused():
        return {"resumed": [], "skipped": "paused"}
    held = supabase.list_held_sms_campaigns()
    if not held:
        return {"resumed": [], "completed": []}
    headroom, _retry, _reason = remaining_global_bird(supabase, effective_bird_settings(supabase))
    if headroom <= 0:
        return {"resumed": [], "completed": []}
    resumed, completed = [], []
    for campaign in held:
        cid = campaign["id"]
        if supabase.count_sms_recipients(cid, status="queued") <= 0:
            _complete(supabase, cid)
            completed.append(cid)
            continue
        supabase.update_sms_campaign(cid, {
            "status": "sending", "held_reason": None, "held_until": None, "next_send_at": None,
        })
        send_bird_sms_campaign.apply_async(args=[cid], countdown=1)
        resumed.append(cid)
    return {"resumed": resumed, "completed": completed}


def reconcile_stuck_bird_sms_campaigns() -> dict:
    """Requeue stale claims and complete stranded sending campaigns."""
    from app.core.bird_sms_store import get_bird_store

    supabase = get_bird_store()
    requeued = 0
    try:
        requeued = supabase.client.rpc("requeue_stale_bird_sms_recipients", {
            "p_timeout_seconds": STALE_CLAIM_TIMEOUT_SECONDS,
        }).execute().data or 0
    except Exception:  # noqa: BLE001
        logger.warning("bird sms stale requeue failed", exc_info=True)
    completed = []
    for campaign in supabase.list_sending_sms_campaigns():
        if supabase.count_sms_recipients(campaign["id"], status="queued") == 0:
            _complete(supabase, campaign["id"])
            completed.append(campaign["id"])
    return {"requeued": requeued, "completed": completed}
