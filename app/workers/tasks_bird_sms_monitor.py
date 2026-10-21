"""Bird SMS circuit breaker & health monitor tasks."""
from __future__ import annotations

import logging

from app.config import STALE_CLAIM_TIMEOUT_SECONDS
from app.workers.celery_app import celery

logger = logging.getLogger(__name__)


@celery.task(name="app.workers.tasks_bird_sms_monitor.evaluate_bird_sms_circuit")
def evaluate_bird_sms_circuit() -> dict:
    from app.core.bird_sms_config import effective_bird_settings
    from app.core.bird_sms_store import get_bird_store
    from app.core.telegram import get_telegram

    store = get_bird_store()
    settings = effective_bird_settings(store)
    max_bounce = float(settings.get("max_hard_bounce_rate") or 0.03)
    tripped = []
    for campaign in store.list_active_sms_campaigns():
        cid = campaign["id"]
        metrics = store.get_sms_campaign_metrics(cid)
        sent = int(metrics.get("sent", 0) or 0)
        if sent == 0:
            continue
        bounces = int(metrics.get("failed", 0) or 0) + int(metrics.get("undelivered", 0) or 0)
        rate = bounces / sent
        if rate <= max_bounce:
            continue
        store.update_sms_campaign(cid, {
            "status": "paused", "circuit_state": "paused", "finished_at": "now()",
        })
        tripped.append(cid)
        get_telegram("bird_sms").send_message(
            "*Bird SMS circuit breaker tripped*\n"
            f"Campaign: {campaign.get('name')} (`{cid}`)\n"
            f"Failure rate {rate:.2%} > {max_bounce:.2%}\n"
            f"Stats: sent={sent}, failed/undelivered={bounces}"
        )
        logger.warning("bird sms circuit tripped for %s: %s", cid, rate)
    return {"tripped": tripped}


@celery.task(name="app.workers.tasks_bird_sms_monitor.reconcile_stuck_bird_sms_campaigns")
def reconcile_stuck_bird_sms_campaigns() -> dict:
    from app.core.bird_sms_store import get_bird_store
    from app.workers.tasks_bird_sms import send_bird_sms_campaign

    store = get_bird_store()
    requeued = 0
    try:
        requeued = store.client.rpc("requeue_stale_bird_sms_recipients", {
            "p_timeout_seconds": STALE_CLAIM_TIMEOUT_SECONDS,
        }).execute().data or 0
    except Exception:  # noqa: BLE001
        logger.warning("bird sms stale requeue failed", exc_info=True)

    redispatched, completed = [], []
    for campaign in store.list_active_sms_campaigns():
        if campaign.get("status") != "sending":
            continue
        cid = campaign["id"]
        queued = store.count_sms_recipients(cid, "queued")
        sending = store.count_sms_recipients(cid, "sending")
        if queued > 0 and campaign.get("circuit_state", "ok") in ("ok", "warning"):
            send_bird_sms_campaign.delay(cid)
            redispatched.append(cid)
        elif queued == 0 and sending == 0:
            store.update_sms_campaign(cid, {"status": "completed", "finished_at": "now()"})
            completed.append(cid)
    return {"requeued": requeued, "redispatched": redispatched, "completed": completed}
