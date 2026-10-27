"""Operator commands for the Telegram bot (/report, /restart).

Telegram only delivers messages while something polls ``getUpdates`` (or a
webhook is configured). This module provides a tiny, chat-allowlisted poller
driven by Celery Beat (or the cron tick) plus the command handlers themselves.

Only the configured chat is allowed to issue commands; updates from any other
chat are acknowledged (offset advanced) but ignored.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_NAME = "webmailer-telegram.state"
_OFFSET_REDIS_KEY = "telegram:update_offset"

USAGE = (
    "🤖 *Campaign commands*\n"
    "`/report <campaign name>` — status and delivery report\n"
    "`/restart <campaign name>` — continue a paused campaign from where it stopped"
)


# --------------------------------------------------------------------------- #
# Offset persistence (Redis when available, state file for cron-only hosts)
# --------------------------------------------------------------------------- #
def _state_path() -> Path:
    base = os.environ.get("CRON_STATE_DIR") or tempfile.gettempdir()
    return Path(base) / _STATE_NAME


def _read_offset() -> int | None:
    try:
        from app.extensions import _RedisHolder

        value = _RedisHolder.get().get(_OFFSET_REDIS_KEY)
        if value is not None:
            return int(value)
        return None
    except Exception:  # noqa: BLE001 - fall back to the file
        pass
    try:
        return int(_state_path().read_text().strip())
    except Exception:  # noqa: BLE001
        return None


def _write_offset(offset: int) -> None:
    try:
        from app.extensions import _RedisHolder

        _RedisHolder.get().set(_OFFSET_REDIS_KEY, str(int(offset)))
        return
    except Exception:  # noqa: BLE001 - fall back to the file
        pass
    try:
        _state_path().write_text(str(int(offset)))
    except Exception:  # noqa: BLE001
        logger.warning("could not persist telegram update offset", exc_info=True)


# --------------------------------------------------------------------------- #
# Command parsing
# --------------------------------------------------------------------------- #
def parse_command(text: str) -> tuple[str, str] | None:
    """Split ``/command[@Bot] rest`` into (command, argument)."""
    raw = str(text or "").strip()
    if not raw.startswith("/"):
        return None
    head, _, rest = raw.partition(" ")
    command = head[1:].split("@", 1)[0].strip().lower()
    if not command:
        return None
    return command, rest.strip()


def clean_argument(argument: str) -> str:
    """Strip surrounding single/double quotes from a campaign name."""
    value = str(argument or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


# --------------------------------------------------------------------------- #
# Report formatting
# --------------------------------------------------------------------------- #
def format_report(payload: dict[str, Any]) -> str:
    from app.core.telegram import escape_markdown

    lines = [
        "📊 *Campaign report*",
        f"Name: {escape_markdown(str(payload.get('name') or ''))}",
        f"ID: `{payload.get('id')}`",
        f"Status: *{payload.get('status')}*",
        f"Circuit: {payload.get('circuit_state')}",
        "",
        f"Sent: {payload.get('sent', 0)} / {payload.get('total_recipients', 0)}",
        f"Queued remaining: {payload.get('queued', 0)}",
        f"Delivered: {payload.get('delivered', 0)}",
        f"Opens: {payload.get('opens', 0)}",
        f"Clicks: {payload.get('clicks', 0)}",
        f"Hard bounces: {payload.get('hard_bounces', 0)}",
        f"Soft bounces: {payload.get('soft_bounces', 0)}",
        f"Complaints: {payload.get('complaints', 0)}",
        f"Unsubscribes: {payload.get('unsubscribes', 0)}",
        f"Failed: {payload.get('failed', 0)}",
        "",
        f"Hard-bounce rate: {float(payload.get('hard_bounce_rate') or 0):.2%}",
        f"Complaint rate: {float(payload.get('complaint_rate') or 0):.2%}",
    ]
    segment_sent = payload.get("segment_sent")
    if segment_sent:
        lines.append(
            "Since restart: "
            f"{segment_sent} sent, {payload.get('segment_hard_bounces', 0)} hard bounces "
            f"({float(payload.get('segment_hard_bounce_rate') or 0):.2%})"
        )
    if payload.get("last_error"):
        lines.append(f"Stopped: {escape_markdown(str(payload['last_error']))}")
    if payload.get("started_at"):
        lines.append(f"Started: {payload['started_at']}")
    if payload.get("finished_at"):
        lines.append(f"Finished: {payload['finished_at']}")
    if payload.get("sending_completed"):
        lines.append("✅ *Sending completed*")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Command handlers
# --------------------------------------------------------------------------- #
def _resolve_campaign(supabase, name: str) -> tuple[dict | None, list[dict]]:
    rows = supabase.find_campaigns_by_name(name)
    if not rows:
        return None, []
    exact = [r for r in rows if str(r.get("name") or "").lower() == name.lower()]
    return (exact or rows)[0], rows


def _ambiguous(name: str, rows: list[dict]) -> str:
    from app.core.telegram import escape_markdown

    lines = [f"⚠️ More than one campaign matches “{escape_markdown(name)}”:"]
    for row in rows[:10]:
        lines.append(
            f"• {escape_markdown(str(row.get('name') or ''))} — {row.get('status')} "
            f"(`{row.get('id')}`)"
        )
    lines.append("Send the exact name (in quotes if it contains spaces).")
    return "\n".join(lines)


def _missing(name: str) -> str:
    from app.core.telegram import escape_markdown

    return f"❓ No campaign named “{escape_markdown(name)}”.\n\n{USAGE}"


def _report_command(name: str) -> str:
    from app.core.campaign_ops import report_payload
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    campaign, rows = _resolve_campaign(supabase, name)
    if campaign is None:
        return _missing(name)
    if len(rows) > 1 and campaign.get("name", "").lower() != name.lower():
        return _ambiguous(name, rows)
    full = supabase.get_campaign(campaign["id"]) or campaign
    return format_report(report_payload(supabase, full))


def _restart_command(name: str) -> str:
    from app.core.campaign_ops import (
        report_payload,
        restart_campaign,
        sending_block_reason,
    )
    from app.core.supabase_client import get_supabase
    from app.core.telegram import escape_markdown

    supabase = get_supabase()
    campaign, rows = _resolve_campaign(supabase, name)
    if campaign is None:
        return _missing(name)
    if len(rows) > 1 and campaign.get("name", "").lower() != name.lower():
        return _ambiguous(name, rows)

    full = supabase.get_campaign(campaign["id"]) or campaign

    blocked = sending_block_reason(supabase)
    if blocked:
        return (
            f"⚠️ {escape_markdown(blocked)}\n\n"
            + format_report(report_payload(supabase, full))
        )

    result = restart_campaign(supabase, full)
    report = result.get("report") or {}

    if not result.get("ok"):
        if result.get("sending_completed"):
            return format_report(report)
        return (
            f"⚠️ {escape_markdown(str(result.get('error') or 'campaign cannot be restarted now'))}\n\n"
            + format_report(report)
        )

    lines = [
        "🔄 *Campaign restarted*",
        f"Name: {escape_markdown(str(report.get('name') or ''))}",
        f"ID: `{report.get('id')}`",
        "Continuing from where it stopped — already-sent contacts are not re-emailed.",
        f"Queued remaining: {report.get('queued', 0)}",
        f"Already sent: {report.get('sent', 0)}",
    ]
    if result.get("skipped"):
        lines.append(f"Excluded from the queue: {result['skipped']}")
    return "\n".join(lines)


def handle_text(text: str) -> str | None:
    """Handle one bot message; return the reply text (or None to stay silent)."""
    parsed = parse_command(text)
    if parsed is None:
        return None
    command, argument = parsed
    name = clean_argument(argument)

    if command in ("report",):
        if not name:
            return "Usage: `/report <campaign name>`"
        return _report_command(name)
    if command in ("restart",):
        if not name:
            return "Usage: `/restart <campaign name>`"
        return _restart_command(name)
    if command in ("help", "start"):
        return USAGE
    # Unknown commands are ignored so unrelated bot chats stay quiet.
    return None


# --------------------------------------------------------------------------- #
# Poller
# --------------------------------------------------------------------------- #
def poll_once(limit: int = 20) -> dict:
    """Fetch and dispatch pending Telegram updates for the operator chat."""
    from app.core.telegram import TelegramClient, resolve_telegram

    config = resolve_telegram("email", use_cache=False)
    if not config.configured:
        return {"skipped": "telegram not configured"}

    client = TelegramClient(config=config)
    offset = _read_offset()
    try:
        updates = client.get_updates(offset=offset, timeout=0)[:limit]
    except Exception as exc:  # noqa: BLE001 - never break the beat tick
        logger.warning("telegram poll failed: %s", exc)
        return {"error": str(exc)}

    if not updates:
        return {"processed": 0, "replied": 0}

    processed = replied = 0
    next_offset = offset
    for update in updates:
        try:
            update_id = int(update.get("update_id"))
        except (TypeError, ValueError):
            continue
        next_offset = max(next_offset or 0, update_id + 1)
        message = update.get("message") or update.get("edited_message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        if config.chat_id and chat_id != str(config.chat_id):
            continue  # ignore commands from other chats, but acknowledge them
        text = message.get("text") or ""
        if not text:
            continue
        try:
            reply = handle_text(text)
        except Exception:  # noqa: BLE001 - one bad command must not stop the poll
            logger.exception("telegram command failed")
            reply = "⚠️ That command failed; check the server logs."
        if reply:
            client.send_message(reply)
            replied += 1
        processed += 1

    if next_offset is not None and (offset is None or next_offset > offset):
        _write_offset(next_offset)
    return {"processed": processed, "replied": replied}


def clear_offset() -> None:
    """Forget the stored update offset (tests / operator reset)."""
    try:
        from app.extensions import _RedisHolder

        _RedisHolder.get().delete(_OFFSET_REDIS_KEY)
    except Exception:  # noqa: BLE001
        pass
    try:
        _state_path().unlink()
    except Exception:  # noqa: BLE001
        pass
