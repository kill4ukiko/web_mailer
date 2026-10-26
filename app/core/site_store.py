"""Persistence helpers for public website accounts and newsletter enrollment."""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.core.supabase_client import get_supabase

logger = logging.getLogger(__name__)

WEBSITE_SUBSCRIBERS_LIST = "Website Subscribers"


def normalize_email(email: str | None) -> str:
    return str(email or "").strip().lower()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def expiry_iso(hours: int) -> str:
    return (now_utc() + timedelta(hours=max(1, int(hours)))).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def token_is_expired(value: object) -> bool:
    parsed = _parse_datetime(value)
    return parsed is None or parsed <= now_utc()


def _table():
    return get_supabase().client.table("site_users")


def get_site_user_by_email(email: str) -> dict | None:
    rows = (
        _table()
        .select("*")
        .eq("email", normalize_email(email))
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def get_site_user_by_id(user_id: str) -> dict | None:
    rows = _table().select("*").eq("id", user_id).limit(1).execute().data or []
    return rows[0] if rows else None


def get_pending_user_by_token(token: str) -> dict | None:
    rows = (
        _table()
        .select("*")
        .eq("verification_token_hash", hash_token(token))
        .eq("status", "pending")
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def create_site_user(data: dict) -> dict:
    payload = {
        **data,
        "email": normalize_email(data.get("email")),
        "status": "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    rows = _table().insert(payload).execute().data or []
    if not rows:
        raise RuntimeError("site user insert returned no row")
    return rows[0]


def set_verification_token(
    user_id: str, token_hash: str, expires_at: str, sent_at: str | None = None
) -> dict | None:
    rows = (
        _table()
        .update(
            {
                "verification_token_hash": token_hash,
                "verification_expires_at": expires_at,
                "verification_sent_at": sent_at or now_iso(),
                "updated_at": now_iso(),
            }
        )
        .eq("id", user_id)
        .eq("status", "pending")
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def activate_site_user(user_id: str, token_hash: str) -> dict | None:
    updated = (
        _table()
        .update(
            {
                "status": "active",
                "email_verified_at": now_iso(),
                "verification_token_hash": None,
                "verification_expires_at": None,
                "updated_at": now_iso(),
            }
        )
        .eq("id", user_id)
        .eq("verification_token_hash", token_hash)
        .eq("status", "pending")
        .execute()
        .data
        or []
    )
    if updated:
        return updated[0]
    current = get_site_user_by_id(user_id)
    if current and current.get("status") == "active" and not current.get("verification_token_hash"):
        return current
    return None


def touch_site_login(user_id: str) -> None:
    _table().update(
        {"last_login_at": now_iso(), "updated_at": now_iso()}
    ).eq("id", user_id).execute()


def set_site_contact(user_id: str, contact_id: str | None) -> None:
    _table().update(
        {"contact_id": contact_id, "updated_at": now_iso()}
    ).eq("id", user_id).execute()


def update_site_consent(
    user_id: str,
    consent: bool,
    *,
    version: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict | None:
    payload: dict[str, object] = {
        "marketing_consent": consent,
        "updated_at": now_iso(),
    }
    if consent:
        payload.update(
            {
                "consent_version": version,
                "consent_at": now_iso(),
                "consent_withdrawn_at": None,
                "consent_ip": ip,
                "consent_user_agent": user_agent,
            }
        )
    else:
        payload["consent_withdrawn_at"] = now_iso()
    rows = _table().update(payload).eq("id", user_id).execute().data or []
    return rows[0] if rows else None


def _get_contact(contact_id: str) -> dict | None:
    rows = (
        get_supabase()
        .client.table("contacts")
        .select("id,email,status,safe_to_send,deleted_at")
        .eq("id", contact_id)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def get_site_contact(email: str) -> dict | None:
    sb = get_supabase()
    found = sb.get_contacts_by_emails([normalize_email(email)])
    row = found.get(normalize_email(email))
    if not row:
        return None
    return _get_contact(row["id"])


def get_website_subscribers_list() -> dict:
    sb = get_supabase()
    wanted = WEBSITE_SUBSCRIBERS_LIST.lower()
    for entry in sb.list_lists():
        if str(entry.get("name") or "").strip().lower() == wanted:
            return entry
    try:
        return sb.create_list(
            WEBSITE_SUBSCRIBERS_LIST,
            "Verified website sign-ups who consented to GoPriLad news emails.",
        )
    except Exception:  # noqa: BLE001 - unique name race or transient DB error
        logger.warning("Website Subscribers list creation failed; retrying lookup")
        for entry in sb.list_lists():
            if str(entry.get("name") or "").strip().lower() == wanted:
                return entry
        raise


def enroll_site_user(user: dict) -> dict:
    if not user.get("marketing_consent"):
        return {"subscribed": False, "reason": "no_consent", "contact_id": user.get("contact_id")}
    email = normalize_email(user.get("email"))
    if not email:
        return {"subscribed": False, "reason": "invalid_email", "contact_id": None}
    sb = get_supabase()
    mapping = sb.ensure_contacts([{"email": email}])
    contact_id = mapping.get(email)
    if not contact_id:
        return {"subscribed": False, "reason": "contact_missing", "contact_id": None}
    contact = _get_contact(contact_id)
    set_site_contact(user["id"], contact_id)
    if (
        not contact
        or contact.get("deleted_at") is not None
        or contact.get("status") != "active"
        or contact.get("safe_to_send") is False
    ):
        return {"subscribed": False, "reason": "suppressed", "contact_id": contact_id}
    subscriber_list = get_website_subscribers_list()
    sb.add_members(subscriber_list["id"], [contact_id])
    return {"subscribed": True, "reason": "active", "contact_id": contact_id}


def revoke_site_user_consent(email: str) -> int:
    """Clear website-marketing consent after a provider-side suppression.

    An unsubscribe/bounce/complaint can arrive from Brevo without the browser
    visiting the site. Keeping ``marketing_consent`` true would make the
    dashboard claim the reader is still subscribed, so mirror the suppression
    onto any matching site account. Returns the number of accounts updated.
    """
    normalized = normalize_email(email)
    if not normalized:
        return 0
    rows = (
        _table()
        .select("id,marketing_consent")
        .eq("email", normalized)
        .limit(10)
        .execute()
        .data
        or []
    )
    updated = 0
    for row in rows:
        if row.get("marketing_consent"):
            update_site_consent(row["id"], False)
            updated += 1
    return updated


def unsubscribe_site_user(user: dict) -> None:
    email = normalize_email(user.get("email"))
    if not email:
        return
    sb = get_supabase()
    contact = get_site_contact(email)
    sb.update_contact_status(email, "unsubscribed")
    if not contact:
        return
    try:
        subscriber_list = get_website_subscribers_list()
        sb.remove_member(subscriber_list["id"], contact["id"])
    except Exception:  # noqa: BLE001 - suppression already stops delivery
        logger.warning("Website Subscribers membership cleanup failed for %s", email)
