"""Supabase client wrapper and typed helper methods.

All read/write operations go through the PostgREST client using the
service-role key so the backend can bypass Row Level Security. Stored
procedures (claim_recipients / suppress_contact) are invoked via RPC.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from supabase import Client, create_client

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def is_uuid(value: object) -> bool:
    """True when ``value`` is a canonical UUID string.

    PostgREST raises ``invalid input syntax for type uuid`` for anything else,
    which used to turn a bad tracking/unsubscribe link into a 500.
    """
    from uuid import UUID

    try:
        UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


# Delivery-lifecycle filter -> stored ``contacts.last_recipient_status`` values.
DELIVERY_FILTERS: dict[str, tuple[str, ...]] = {
    "sent": ("sent",),
    "delivered": ("delivered",),
    "opened": ("opened",),
    "clicked": ("clicked",),
    "bounced": ("hard_bounce", "soft_bounce"),
    "complained": ("complained",),
    "unsubscribed": ("unsubscribed",),
}

# SMS delivery-lifecycle filter -> stored ``sms_contacts.last_recipient_status``.
SMS_DELIVERY_FILTERS: dict[str, tuple[str, ...]] = {
    "sent": ("sent", "accepted", "delivered", "replied"),
    "delivered": ("delivered", "replied"),
    "replied": ("replied",),
    "bounced": ("hard_bounce", "soft_bounce"),
    "blacklisted": ("blacklisted",),
    "unsubscribed": ("unsubscribed",),
}


class SupabaseClientError(RuntimeError):
    """Raised when a Supabase operation fails."""


class SupabaseWrapper:
    """Thin, typed wrapper around the supabase-py client."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client: Client = create_client(
            self.settings.supabase_url,
            self.settings.supabase_service_role_key or self.settings.supabase_anon_key,
        )

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #
    # Columns that must never be reset by an import/re-add upsert: suppression
    # (status + safe_to_send) and the soft-delete/lifecycle bookkeeping.
    _PRESERVED_CONTACT_COLUMNS = (
        "status",
        "safe_to_send",
        "deleted_at",
        "last_recipient_status",
        "last_event_at",
        "soft_bounce_count",
        "last_soft_bounce_at",
    )

    def list_contacts(
        self,
        page: int = 1,
        limit: int = 50,
        status: str | None = None,
        include_deleted: bool = False,
        delivery: str | None = None,
    ) -> dict[str, Any]:
        def _base(query):
            if not include_deleted:
                query = query.is_("deleted_at", "null")
            if status:
                query = query.eq("status", status)
            if delivery:
                values = DELIVERY_FILTERS.get(delivery)
                if len(values) == 1:
                    query = query.eq("last_recipient_status", values[0])
                else:
                    query = query.in_("last_recipient_status", values)
            return query

        query = _base(
            self.client.table("contacts").select("*").order("created_at", desc=True)
        ).range((page - 1) * limit, page * limit - 1)
        data = query.execute().data

        count_query = _base(self.client.table("contacts").select("id", count="exact"))
        total = count_query.execute().count or 0
        return {"page": page, "limit": limit, "total": total, "items": data or []}

    @classmethod
    def _profile_payload(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Drop suppression/lifecycle columns so an upsert preserves them.

        PostgREST only updates the columns present in the payload, so omitting
        these lets an existing (suppressed, maybe soft-deleted) contact keep its
        state while a brand-new email picks up the DB column defaults.
        """
        return {k: v for k, v in data.items() if k not in cls._PRESERVED_CONTACT_COLUMNS}

    def create_contact(self, data: dict[str, Any]) -> dict[str, Any] | None:
        email = str(data["email"]).lower().strip()
        payload = self._profile_payload({**data, "email": email})
        return self.client.table("contacts").upsert(payload, on_conflict="email").execute().data

    def bulk_insert_contacts(self, contacts: Iterable[dict[str, Any]]) -> tuple[int, list[str]]:
        """Upsert a batch of contacts keyed on email.

        Existing contacts keep their suppression state (status / safe_to_send);
        only profile fields are updated. Returns (imported_count, invalid_rows).
        """
        rows = [self._profile_payload(dict(c)) for c in contacts]
        if not rows:
            return 0, []
        imported = self.client.table("contacts").upsert(
            rows, on_conflict="email"
        ).execute().data
        return len(imported or []), []

    def update_contact_status(self, email: str, status: str) -> None:
        """Set a contact's status; suppression follows (non-active => unsafe)."""
        self.client.table("contacts").update(
            {"status": status, "safe_to_send": status == "active", "updated_at": "now()"}
        ).eq("email", email.lower().strip()).execute()

    def soft_delete_contacts(self, contact_ids: Iterable[str]) -> int:
        """Hide contacts without losing their suppression/lifecycle state."""
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        result = (
            self.client.table("contacts")
            .update({"deleted_at": "now()", "updated_at": "now()"})
            .in_("id", ids)
            .execute()
        )
        return len(result.data or [])

    def restore_contacts(self, contact_ids: Iterable[str]) -> int:
        """Un-hide soft-deleted contacts (their status/suppression is kept)."""
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        result = (
            self.client.table("contacts")
            .update({"deleted_at": None, "updated_at": "now()"})
            .in_("id", ids)
            .execute()
        )
        return len(result.data or [])

    def touch_contact_lifecycle(
        self, email: str, status: str, when: str | None = None
    ) -> None:
        """Record a contact's newest delivery outcome (for badges/filters)."""
        if not email:
            return
        self.client.table("contacts").update({
            "last_recipient_status": status,
            "last_event_at": when or "now()",
        }).eq("email", email.lower().strip()).execute()

    def bulk_touch_contact_lifecycle(self, emails: Iterable[str], status: str) -> None:
        """Record a delivery outcome for many contacts at once (by email)."""
        cleaned = [e.lower().strip() for e in emails if e and str(e).strip()]
        if not cleaned:
            return
        self.client.table("contacts").update({
            "last_recipient_status": status,
            "last_event_at": "now()",
        }).in_("email", cleaned).execute()

    def register_soft_bounce(self, email: str, threshold: int) -> bool:
        """Bump a contact's soft-bounce tally; suppress at/above ``threshold``.

        Returns True when the contact was suppressed by this event. Used by the
        per-campaign ``threshold`` soft-bounce policy.
        """
        email = email.lower().strip()
        rows = (
            self.client.table("contacts")
            .select("id,soft_bounce_count")
            .eq("email", email)
            .execute()
            .data
            or []
        )
        if not rows:
            return False
        count = int(rows[0].get("soft_bounce_count") or 0) + 1
        suppressed = threshold > 0 and count >= threshold
        update: dict[str, Any] = {
            "soft_bounce_count": count,
            "last_soft_bounce_at": "now()",
        }
        if suppressed:
            update["status"] = "bounced_soft"
            update["safe_to_send"] = False
        self.client.table("contacts").update(update).eq("email", email).execute()
        return suppressed

    def register_hard_bounce(self, email: str, threshold: int) -> bool:
        """Bump a contact's hard-bounce tally; suppress at/above ``threshold``.

        Returns True when the contact was suppressed by this event. Used by the
        per-campaign ``threshold`` hard-bounce policy. The contact is always
        suppressed once the threshold is reached (hard bounces are permanent).
        """
        email = email.lower().strip()
        rows = (
            self.client.table("contacts")
            .select("id,hard_bounce_count")
            .eq("email", email)
            .execute()
            .data
            or []
        )
        if not rows:
            return False
        count = int(rows[0].get("hard_bounce_count") or 0) + 1
        suppressed = threshold > 0 and count >= threshold
        update: dict[str, Any] = {
            "hard_bounce_count": count,
            "last_hard_bounce_at": "now()",
        }
        if suppressed:
            update["status"] = "bounced_hard"
            update["safe_to_send"] = False
        self.client.table("contacts").update(update).eq("email", email).execute()
        return suppressed

    def delete_contacts(self, contact_ids: Iterable[str]) -> int:
        """Delete several contacts by id; return how many rows were removed.

        List memberships and campaign recipients cascade in the database.
        """
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        result = self.client.table("contacts").delete().in_("id", ids).execute()
        return len(result.data or [])

    def update_contact(self, contact_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """Patch a single contact's editable fields."""
        payload = {k: v for k, v in data.items() if v is not None or k == "metadata"}
        if "email" in payload and payload["email"] is not None:
            payload["email"] = str(payload["email"]).lower().strip()
        payload["updated_at"] = "now()"
        row = (
            self.client.table("contacts").update(payload).eq("id", contact_id).execute().data
        )
        return row[0] if row else None

    def search_contacts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Find contacts whose email / name contains ``query`` (for pickers)."""
        q = (query or "").strip()
        base = self.client.table("contacts").select(
            "id,email,first_name,last_name,status"
        )
        if q:
            escaped = q.replace(",", " ")
            base = base.or_(
                f"email.ilike.%{escaped}%,first_name.ilike.%{escaped}%,"
                f"last_name.ilike.%{escaped}%"
            )
        return base.order("created_at", desc=True).limit(limit).execute().data or []

    def ensure_contacts(self, rows: Iterable[dict[str, Any]]) -> dict[str, str]:
        """Create any missing contacts, returning an ``email -> id`` map.

        Unknown emails are created as ``active`` contacts (DB default) so that
        list imports, manual entries and campaign extra-recipients never
        silently drop a row. Existing contacts are only updated on their profile
        fields: suppression (status / safe_to_send) and soft-delete state are
        always preserved, so re-adding a bounced address can never reactivate it.
        """
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            email = str(row.get("email") or "").lower().strip()
            if not email or email in seen:
                continue
            seen.add(email)
            entry: dict[str, Any] = {"email": email}
            for field in ("first_name", "last_name", "timezone", "metadata"):
                if row.get(field) is not None:
                    entry[field] = row[field]
            prepared.append(entry)

        if not prepared:
            return {}
        data = (
            self.client.table("contacts")
            .upsert(prepared, on_conflict="email")
            .execute()
            .data
            or []
        )
        return {r["email"]: r["id"] for r in data if r.get("email") and r.get("id")}

    def get_contacts_by_emails(
        self, emails: Iterable[str]
    ) -> dict[str, dict[str, Any]]:
        """Look up contacts by normalized email (used by suppression import)."""
        cleaned = sorted({
            str(email).lower().strip()
            for email in emails
            if email and str(email).strip()
        })
        found: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(cleaned), 100):
            chunk = cleaned[offset:offset + 100]
            rows = (
                self.client.table("contacts")
                .select("id,email,status")
                .in_("email", chunk)
                .execute()
                .data
                or []
            )
            for row in rows:
                if row.get("email"):
                    found[row["email"]] = row
        return found

    def bulk_suppress_contacts(self, emails: Iterable[str], reason: str) -> int:
        """Apply a suppression status to many existing contacts."""
        cleaned = sorted({
            str(email).lower().strip()
            for email in emails
            if email and str(email).strip()
        })
        payload = {
            "status": reason,
            "safe_to_send": False,
            "updated_at": "now()",
        }
        for offset in range(0, len(cleaned), 100):
            chunk = cleaned[offset:offset + 100]
            self.client.table("contacts").update(payload).in_("email", chunk).execute()
        return len(cleaned)

    def delete_contact(self, contact_id: str) -> bool:
        """Delete a contact by id (recipients cascade)."""
        result = (
            self.client.table("contacts").delete().eq("id", contact_id).execute()
        )
        return bool(result.data)

    # ------------------------------------------------------------------ #
    # Campaigns
    # ------------------------------------------------------------------ #
    def create_campaign(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self.client.table("campaigns").insert(data).execute().data
        if not row:
            raise SupabaseClientError("campaign insert returned no row")
        return row[0]

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        data = self.client.table("campaigns").select("*").eq("id", campaign_id).execute().data
        return data[0] if data else None

    def find_campaigns_by_name(self, name: str) -> list[dict[str, Any]]:
        """Find campaigns whose name matches ``name`` (case-insensitive).

        An exact match wins; otherwise a contains match is returned so Telegram
        commands can work with a short name. Results are newest first.
        """
        query = (name or "").strip()
        if not query:
            return []
        rows = (
            self.client.table("campaigns")
            .select("id,name,status,circuit_state")
            .ilike("name", f"%{query}%")
            .limit(10)
            .execute()
            .data
            or []
        )
        exact = [r for r in rows if str(r.get("name") or "").lower() == query.lower()]
        return exact or rows

    def update_campaign(self, campaign_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        row = self.client.table("campaigns").update(data).eq("id", campaign_id).execute().data
        return row[0] if row else None

    def delete_campaign(self, campaign_id: str) -> bool:
        """Delete a campaign (recipients and events cascade)."""
        result = (
            self.client.table("campaigns").delete().eq("id", campaign_id).execute()
        )
        return bool(result.data)

    def add_recipients(self, campaign_id: str, contact_rows: Iterable[dict[str, Any]]) -> int:
        rows = [
            {"campaign_id": campaign_id, "contact_id": contact_id}
            for contact_id in self._contact_ids_for(contact_rows)
        ]
        if not rows:
            return 0
        result = self.client.table("campaign_recipients").upsert(
            rows, on_conflict="campaign_id,contact_id"
        ).execute()
        return len(result.data or [])

    def _contact_ids_for(self, rows: Iterable[dict[str, Any]]) -> list[str]:
        rows = list(rows)
        if not rows:
            return []
        # Unknown emails are created on the fly so no recipient is dropped.
        return list(self.ensure_contacts(rows).values())

    def list_recipients(
        self, campaign_id: str, status: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        query = self.client.table("campaign_recipients").select("*").eq(
            "campaign_id", campaign_id
        ).limit(limit)
        if status:
            query = query.eq("status", status)
        return query.execute().data or []

    def update_recipient(self, recipient_id: str, data: dict[str, Any]) -> int:
        """Update one recipient; return how many rows actually changed.

        Callers must not report success when this returns 0 (the row may have
        been deleted with its campaign).
        """
        result = (
            self.client.table("campaign_recipients")
            .update(data)
            .eq("id", recipient_id)
            .execute()
        )
        return len(result.data or [])

    def get_recipient_unsubscribe_context(
        self, recipient_id: str, strict: bool = False
    ) -> dict[str, Any] | None:
        """Return the recipient status and linked contact email for opt-outs.

        A malformed id returns ``None`` instead of raising PostgREST's
        ``invalid input syntax for type uuid`` error. Pass ``strict=True`` when
        the caller must distinguish "not found" from a transient lookup failure
        (unsubscribe flows use this to show an honest retry page instead of
        claiming the subscription does not exist).
        """
        try:
            rows = (
                self.client.table("campaign_recipients")
                .select("id,status,contact_id,campaign_id")
                .eq("id", recipient_id)
                .limit(1)
                .execute()
                .data
            )
            if not rows:
                return None
            row = rows[0]
            email = None
            contact_id = row.get("contact_id")
            if contact_id:
                contacts = (
                    self.client.table("contacts")
                    .select("email")
                    .eq("id", contact_id)
                    .limit(1)
                    .execute()
                    .data
                )
                if contacts:
                    email = contacts[0].get("email")
            return {
                "status": row.get("status"),
                "contact_id": contact_id,
                "campaign_id": row.get("campaign_id"),
                "email": email,
            }
        except Exception as exc:  # noqa: BLE001 - malformed id / transient DB error
            if strict:
                raise
            logger.warning(
                "unsubscribe context lookup failed for %r: %s", recipient_id, exc
            )
            return None

    def bulk_update_recipients(self, recipient_ids: Iterable[str], data: dict[str, Any]) -> int:
        ids = list(recipient_ids)
        if not ids:
            return 0
        result = (
            self.client.table("campaign_recipients")
            .update(data)
            .in_("id", ids)
            .execute()
        )
        return len(result.data or [])

    # ------------------------------------------------------------------ #
    # Stored procedures (atomic operations)
    # ------------------------------------------------------------------ #
    def claim_recipients(self, campaign_id: str, limit: int) -> list[dict[str, Any]]:
        """Atomically reserve `limit` queued recipients via row-level locking."""
        data = self.client.rpc("claim_recipients", {
            "p_campaign_id": campaign_id,
            "p_limit": limit,
        }).execute().data
        return data or []

    def suppress_contact(self, email: str, reason: str) -> None:
        self.client.rpc("suppress_contact", {
            "p_email": email,
            "p_reason": reason,
        }).execute()

    # ------------------------------------------------------------------ #
    # Events & stats
    # ------------------------------------------------------------------ #
    def record_event(
        self,
        event_type: str,
        campaign_id: str | None = None,
        recipient_id: str | None = None,
        url_clicked: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist a tracking/webhook event; best-effort and never raising.

        ``campaign_id`` / ``recipient_id`` are foreign keys, so a link or webhook
        for a campaign that has since been deleted would otherwise fail the whole
        insert. On failure we retry once without those columns so the event is
        still stored anonymously, and we never propagate the error to callers.
        """
        payload: dict[str, Any] = {
            "event_type": event_type,
            "url_clicked": url_clicked,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "raw_payload": raw_payload or {},
        }
        if campaign_id:
            payload["campaign_id"] = campaign_id
        if recipient_id:
            payload["recipient_id"] = recipient_id

        try:
            self.client.table("email_events").insert(payload).execute()
            return
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("record_event failed (%s); retrying without FKs", exc)

        anonymous = {
            k: v for k, v in payload.items() if k not in ("campaign_id", "recipient_id")
        }
        try:
            self.client.table("email_events").insert(anonymous).execute()
        except Exception as exc:  # noqa: BLE001 - give up, but never raise
            logger.error("record_event could not be stored: %s", exc)

    def get_campaign_metrics(self, campaign_id: str) -> dict[str, int | float]:
        """Aggregate delivery metrics for the circuit breaker."""
        campaigns = self.client.table("campaigns").select("stats").eq("id", campaign_id).execute().data
        stats = (campaigns[0] or {}).get("stats", {}) if campaigns else {}
        return {
            "sent": stats.get("sent", 0),
            "delivered": stats.get("delivered", 0),
            "opens": stats.get("opens", 0),
            "clicks": stats.get("clicks", 0),
            "bounces": stats.get("bounces", 0),
            "complaints": stats.get("complaints", 0),
        }

    # Counters that live in campaigns.stats (JSONB). Reject anything else so a
    # bad field name can never silently create an unknown key.
    STATS_FIELDS = frozenset({
        "sent", "delivered", "opens", "clicks", "bounces", "complaints",
        "unsubscribes", "failed", "hard_bounce", "soft_bounce",
    })

    def increment_stats(self, campaign_id: str, field: str, delta: int = 1) -> None:
        """Atomically add ``delta`` to ``campaigns.stats[field]``.

        Uses the ``increment_campaign_stat`` SQL function so concurrent
        webhooks/tracking events cannot lose updates. Falls back to the legacy
        read-modify-write when the function has not been deployed yet.
        """
        if field not in self.STATS_FIELDS:
            raise ValueError(f"unknown stats field: {field!r}")
        try:
            self.client.rpc(
                "increment_campaign_stat",
                {"p_campaign_id": campaign_id, "p_field": field, "p_delta": delta},
            ).execute()
            return
        except Exception as exc:  # noqa: BLE001 - fall back on older schemas
            logger.warning("atomic stats RPC unavailable (%s); using fallback", exc)

        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            return
        stats = dict(campaign.get("stats") or {})
        stats[field] = stats.get(field, 0) + delta
        self.client.table("campaigns").update({"stats": stats}).eq("id", campaign_id).execute()

    def list_active_campaigns(self) -> list[dict[str, Any]]:
        data = self.client.table("campaigns").select("*").in_(
            "status", ["scheduled", "sending"]
        ).execute().data
        return data or []

    def list_sending_campaigns(self) -> list[dict[str, Any]]:
        """Campaigns currently transmitting (used for budget fair-share)."""
        data = (
            self.client.table("campaigns")
            .select("*")
            .eq("status", "sending")
            .execute()
            .data
        )
        return data or []

    def list_held_campaigns(self) -> list[dict[str, Any]]:
        """Campaigns paused by an exhausted send cap (awaiting reset)."""
        data = (
            self.client.table("campaigns")
            .select("*")
            .eq("status", "held")
            .execute()
            .data
        )
        return data or []

    def list_engagement_campaigns(self, hours: int = 24) -> list[dict[str, Any]]:
        """Sending campaigns plus those finished within the last ``hours``.

        Opens/clicks frequently arrive after a campaign completes, so engagement
        digests must keep watching recently finished campaigns too.
        """
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = (
            self.client.table("campaigns")
            .select("*")
            .in_("status", ["sending", "completed"])
            .execute()
            .data
            or []
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") == "sending":
                out.append(row)
            elif str(row.get("finished_at") or "") >= since:
                out.append(row)
        return out

    def list_recently_completed_campaigns(
        self, hours: int = 24
    ) -> list[dict[str, Any]]:
        """Completed campaigns finished recently and not yet health-checked.

        Short campaigns can finish between two monitor ticks, so the circuit
        breaker must inspect them after the fact. A breach claims the row with
        ``reputation_checked_at`` immediately; a healthy campaign stays eligible
        until the monitor settles it after 24 hours.
        """
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        try:
            rows = (
                self.client.table("campaigns")
                .select("*")
                .eq("status", "completed")
                .is_("reputation_checked_at", "null")
                .execute()
                .data
                or []
            )
        except Exception as exc:  # noqa: BLE001 - pre-migration schema fallback
            logger.warning(
                "reputation scan unavailable (apply 20_safety_fixes.sql): %s", exc
            )
            return []
        return [
            row
            for row in rows
            if row.get("reputation_checked_at") is None
            and str(row.get("finished_at") or "") >= since
        ]

    def mark_campaign_reputation_checked(
        self,
        campaign_id: str,
        *,
        healthy: bool,
        last_error: str | None = None,
    ) -> bool:
        """Claim the one-time health check for a completed campaign.

        Returns ``False`` when another worker already claimed it, so alerts are
        never duplicated.
        """
        payload: dict[str, Any] = {"reputation_checked_at": "now()"}
        if not healthy:
            payload.update({
                "circuit_state": "paused",
                "last_error": (last_error or "")[:1000],
            })
        rows = (
            self.client.table("campaigns")
            .update(payload)
            .eq("id", campaign_id)
            .is_("reputation_checked_at", "null")
            .execute()
            .data
            or []
        )
        return bool(rows)

    def set_priority(self, campaign_id: str, priority: str) -> dict[str, Any] | None:
        """Set a campaign's priority ('normal' | 'high')."""
        row = (
            self.client.table("campaigns")
            .update({"priority": priority})
            .eq("id", campaign_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def count_recipients(self, campaign_id: str, status: str | None = None) -> int:
        query = self.client.table("campaign_recipients").select(
            "id", count="exact"
        ).eq("campaign_id", campaign_id)
        if status:
            query = query.eq("status", status)
        return query.execute().count or 0

    def count_sent_since(self, since_iso: str) -> int:
        """Count recipients sent at/after ``since_iso`` across all campaigns.

        Used to enforce the global hourly / daily send caps.
        """
        query = (
            self.client.table("campaign_recipients")
            .select("id", count="exact")
            .gte("sent_at", since_iso)
        )
        return query.execute().count or 0

    def list_due_scheduled(self) -> list[dict[str, Any]]:
        """Scheduled campaigns whose schedule_at has arrived (or is unset)."""
        from app.core.scheduling import is_due

        rows = (
            self.client.table("campaigns")
            .select("*")
            .eq("status", "scheduled")
            .execute()
            .data
            or []
        )
        return [row for row in rows if is_due(row.get("schedule_at"))]

    def requeue_stale_recipients(self, timeout_seconds: int = 900) -> int:
        """Requeue recipients abandoned by a crashed worker (via RPC)."""
        result = self.client.rpc(
            "requeue_stale_recipients", {"p_timeout_seconds": timeout_seconds}
        ).execute()
        return int(result.data or 0)

    def add_recipient_ids(self, campaign_id: str, contact_ids: Iterable[str]) -> int:
        """Attach recipients directly by contact id (used for list targeting)."""
        rows = [
            {"campaign_id": campaign_id, "contact_id": cid}
            for cid in set(contact_ids)
        ]
        if not rows:
            return 0
        result = self.client.table("campaign_recipients").upsert(
            rows, on_conflict="campaign_id,contact_id"
        ).execute()
        return len(result.data or [])

    # ------------------------------------------------------------------ #
    # Campaign reuse (clone / resend) lineage
    # ------------------------------------------------------------------ #
    def thread_root(self, campaign_id: str) -> str | None:
        """The root id of the campaign's reuse lineage (self when unset)."""
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            return None
        return campaign.get("reuse_root_id") or campaign["id"]

    def thread_campaign_ids(self, root_id: str) -> list[str]:
        """Every campaign id sharing ``root_id`` (the root included)."""
        rows = (
            self.client.table("campaigns")
            .select("id")
            .eq("reuse_root_id", root_id)
            .execute()
            .data
            or []
        )
        ids = {r["id"] for r in rows}
        ids.add(root_id)
        return sorted(ids)

    def thread_contact_ids(self, root_id: str) -> list[str]:
        """Contact ids already targeted anywhere in the reuse lineage."""
        campaign_ids = self.thread_campaign_ids(root_id)
        if not campaign_ids:
            return []
        rows = (
            self.client.table("campaign_recipients")
            .select("contact_id")
            .in_("campaign_id", campaign_ids)
            .execute()
            .data
            or []
        )
        return sorted({r["contact_id"] for r in rows})

    def campaign_target_contact_ids(self, campaign_id: str) -> list[str]:
        """The contacts a campaign currently targets.

        Uses the campaign's contact lists when present, otherwise every active
        contact (the "send to all active" convenience default).
        """
        lists = self.list_campaign_lists(campaign_id)
        if lists:
            return sorted(self.contact_ids_for_lists([l["id"] for l in lists]))
        data = self.list_contacts(page=1, limit=100000)
        return sorted(c["id"] for c in data["items"] if c.get("status") == "active")

    def reset_recipients(
        self, campaign_id: str, contact_ids: Iterable[str] | None = None
    ) -> int:
        """Move recipients back to ``queued`` for a resend (clears timestamps)."""
        payload = {
            "status": "queued",
            "sent_at": None,
            "claimed_at": None,
            "opened_at": None,
            "clicked_at": None,
            "last_error": None,
        }
        query = (
            self.client.table("campaign_recipients")
            .update(payload)
            .eq("campaign_id", campaign_id)
        )
        if contact_ids is not None:
            ids = list(contact_ids)
            if not ids:
                return 0
            query = query.in_("contact_id", ids)
        result = query.execute()
        return len(result.data or [])

    def clear_duplicate_last_names(self) -> int:
        """Clear ``last_name`` wherever it duplicates ``first_name``.

        Returns the number of contacts cleaned. Comparison is case-insensitive
        and ignores surrounding whitespace.
        """
        rows = (
            self.client.table("contacts")
            .select("id,first_name,last_name")
            .execute()
            .data
            or []
        )
        ids = [
            r["id"]
            for r in rows
            if r.get("first_name")
            and r.get("last_name")
            and str(r["first_name"]).strip().lower() == str(r["last_name"]).strip().lower()
        ]
        if not ids:
            return 0
        self.client.table("contacts").update(
            {"last_name": None, "updated_at": "now()"}
        ).in_("id", ids).execute()
        return len(ids)

    # ------------------------------------------------------------------ #
    # Contact lists (reusable groups, many-to-many with campaigns)
    # ------------------------------------------------------------------ #
    def list_lists(self) -> list[dict[str, Any]]:
        lists = (
            self.client.table("contact_lists").select("*").order("name").execute().data
            or []
        )
        members = (
            self.client.table("contact_list_members").select("list_id").execute().data
            or []
        )
        counts: dict[str, int] = {}
        for row in members:
            counts[row["list_id"]] = counts.get(row["list_id"], 0) + 1
        for entry in lists:
            entry["member_count"] = counts.get(entry["id"], 0)
        return lists

    def create_list(self, name: str, description: str | None = None) -> dict[str, Any]:
        row = (
            self.client.table("contact_lists")
            .insert({"name": name, "description": description, "created_at": "now()"})
            .execute()
            .data
        )
        if not row:
            raise SupabaseClientError("contact_list insert returned no row")
        return row[0]

    def get_list(self, list_id: str) -> dict[str, Any] | None:
        data = (
            self.client.table("contact_lists").select("*").eq("id", list_id).execute().data
        )
        return data[0] if data else None

    def update_list(self, list_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        row = (
            self.client.table("contact_lists")
            .update({**data, "updated_at": "now()"})
            .eq("id", list_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def delete_list(self, list_id: str) -> bool:
        result = (
            self.client.table("contact_lists").delete().eq("id", list_id).execute()
        )
        return bool(result.data)

    def list_members(self, list_id: str) -> list[dict[str, Any]]:
        memberships = (
            self.client.table("contact_list_members")
            .select("contact_id,added_at")
            .eq("list_id", list_id)
            .execute()
            .data
            or []
        )
        ids = [m["contact_id"] for m in memberships]
        if not ids:
            return []
        contacts = (
            self.client.table("contacts")
            .select("id,email,first_name,last_name,status")
            .in_("id", ids)
            .execute()
            .data
            or []
        )
        by_id = {c["id"]: c for c in contacts}
        added = {m["contact_id"]: m.get("added_at") for m in memberships}
        return [
            {**by_id[cid], "added_at": added.get(cid)}
            for cid in ids
            if cid in by_id
        ]

    def add_members(self, list_id: str, contact_ids: Iterable[str]) -> int:
        rows = [
            {"list_id": list_id, "contact_id": cid} for cid in set(contact_ids)
        ]
        if not rows:
            return 0
        result = self.client.table("contact_list_members").upsert(
            rows, on_conflict="list_id,contact_id"
        ).execute()
        return len(result.data or [])

    def add_members_by_email(self, list_id: str, emails: Iterable[str]) -> int:
        """Add members by email, creating unknown contacts first."""
        rows = [{"email": str(e)} for e in emails if e and str(e).strip()]
        if not rows:
            return 0
        return self.add_members(list_id, list(self.ensure_contacts(rows).values()))

    def remove_member(self, list_id: str, contact_id: str) -> bool:
        result = (
            self.client.table("contact_list_members")
            .delete()
            .eq("list_id", list_id)
            .eq("contact_id", contact_id)
            .execute()
        )
        return bool(result.data)

    def contact_ids_for_lists(self, list_ids: Iterable[str]) -> list[str]:
        ids = list({lid for lid in list_ids if lid})
        if not ids:
            return []
        rows = (
            self.client.table("contact_list_members")
            .select("contact_id")
            .in_("list_id", ids)
            .execute()
            .data
            or []
        )
        return list({r["contact_id"] for r in rows})

    def add_campaign_lists(self, campaign_id: str, list_ids: Iterable[str]) -> int:
        rows = [
            {"campaign_id": campaign_id, "list_id": lid} for lid in set(list_ids)
        ]
        if not rows:
            return 0
        result = self.client.table("campaign_lists").upsert(
            rows, on_conflict="campaign_id,list_id"
        ).execute()
        return len(result.data or [])

    def list_campaign_lists(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = (
            self.client.table("campaign_lists")
            .select("list_id")
            .eq("campaign_id", campaign_id)
            .execute()
            .data
            or []
        )
        ids = [r["list_id"] for r in rows]
        if not ids:
            return []
        return (
            self.client.table("contact_lists").select("*").in_("id", ids).execute().data
            or []
        )

    def replace_campaign_lists(self, campaign_id: str, list_ids: Iterable[str]) -> None:
        """Set the exact set of lists a campaign targets."""
        self.client.table("campaign_lists").delete().eq(
            "campaign_id", campaign_id
        ).execute()
        self.add_campaign_lists(campaign_id, list_ids)

    def remove_campaign_list(self, campaign_id: str, list_id: str) -> bool:
        result = (
            self.client.table("campaign_lists")
            .delete()
            .eq("campaign_id", campaign_id)
            .eq("list_id", list_id)
            .execute()
        )
        return bool(result.data)

    # ------------------------------------------------------------------ #
    # Email templates
    # ------------------------------------------------------------------ #
    def list_templates(self) -> list[dict[str, Any]]:
        return (
            self.client.table("email_templates")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        data = (
            self.client.table("email_templates")
            .select("*")
            .eq("id", template_id)
            .execute()
            .data
        )
        return data[0] if data else None

    def create_template(
        self,
        name: str,
        subject: str,
        html_content: str,
        plain_text_content: str | None = None,
    ) -> dict[str, Any]:
        row = (
            self.client.table("email_templates")
            .insert({
                "name": name,
                "subject": subject,
                "html_content": html_content,
                "plain_text_content": plain_text_content,
            })
            .execute()
            .data
        )
        if not row:
            raise SupabaseClientError("email_template insert returned no row")
        return row[0]

    def update_template(
        self, template_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = (
            self.client.table("email_templates")
            .update({**data, "updated_at": "now()"})
            .eq("id", template_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def delete_template(self, template_id: str) -> bool:
        result = (
            self.client.table("email_templates")
            .delete()
            .eq("id", template_id)
            .execute()
        )
        return bool(result.data)

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def get_settings_row(self) -> dict[str, Any] | None:
        data = self.client.table("mailer_settings").select("*").limit(1).execute().data
        return data[0] if data else None

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update the single mailer_settings row, creating it if absent.

        Sender identity columns are NOT NULL, so a fresh row is seeded from
        the app config when it does not exist yet.
        """
        row = self.get_settings_row()
        payload = {**data, "updated_at": "now()"}
        if row:
            updated = (
                self.client.table("mailer_settings")
                .update(payload)
                .eq("id", row["id"])
                .execute()
                .data
            )
            return updated[0] if updated else None

        payload.setdefault("sender_email", self.settings.default_sender_email)
        payload.setdefault("sender_name", self.settings.default_sender_name)
        payload.setdefault("sender_domain", self.settings.default_sender_domain)
        inserted = self.client.table("mailer_settings").insert(payload).execute().data
        return inserted[0] if inserted else None

    # ------------------------------------------------------------------ #
    # SMS: contacts (phone-keyed, fully isolated from email contacts)
    # ------------------------------------------------------------------ #
    # Columns an SMS import/re-add must never reset (suppression + lifecycle).
    _PRESERVED_SMS_CONTACT_COLUMNS = (
        "status",
        "safe_to_send",
        "deleted_at",
        "last_recipient_status",
        "last_event_at",
        "soft_bounce_count",
        "last_soft_bounce_at",
    )

    @classmethod
    def _sms_profile_payload(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Drop suppression/lifecycle columns so an upsert preserves them."""
        return {k: v for k, v in data.items() if k not in cls._PRESERVED_SMS_CONTACT_COLUMNS}

    def list_sms_contacts(
        self,
        page: int = 1,
        limit: int = 50,
        status: str | None = None,
        include_deleted: bool = False,
        delivery: str | None = None,
    ) -> dict[str, Any]:
        def _base(query):
            if not include_deleted:
                query = query.is_("deleted_at", "null")
            if status:
                query = query.eq("status", status)
            if delivery:
                values = SMS_DELIVERY_FILTERS.get(delivery)
                if values:
                    if len(values) == 1:
                        query = query.eq("last_recipient_status", values[0])
                    else:
                        query = query.in_("last_recipient_status", values)
            return query

        query = _base(
            self.client.table("sms_contacts").select("*").order("created_at", desc=True)
        ).range((page - 1) * limit, page * limit - 1)
        data = query.execute().data

        count_query = _base(self.client.table("sms_contacts").select("id", count="exact"))
        total = count_query.execute().count or 0
        return {"page": page, "limit": limit, "total": total, "items": data or []}

    def create_sms_contact(self, data: dict[str, Any]) -> dict[str, Any] | None:
        phone = str(data["phone"]).strip()
        payload = self._sms_profile_payload({**data, "phone": phone})
        return (
            self.client.table("sms_contacts")
            .upsert(payload, on_conflict="phone")
            .execute()
            .data
        )

    def bulk_insert_sms_contacts(
        self, contacts: Iterable[dict[str, Any]]
    ) -> tuple[int, list[str]]:
        """Upsert phone-keyed contacts; existing suppression is preserved."""
        rows = [self._sms_profile_payload(dict(c)) for c in contacts]
        if not rows:
            return 0, []
        imported = (
            self.client.table("sms_contacts")
            .upsert(rows, on_conflict="phone")
            .execute()
            .data
        )
        return len(imported or []), []

    def update_sms_contact_status(self, phone: str, status: str) -> None:
        self.client.table("sms_contacts").update(
            {"status": status, "safe_to_send": status == "active", "updated_at": "now()"}
        ).eq("phone", str(phone).strip()).execute()

    def soft_delete_sms_contacts(self, contact_ids: Iterable[str]) -> int:
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        result = (
            self.client.table("sms_contacts")
            .update({"deleted_at": "now()", "updated_at": "now()"})
            .in_("id", ids)
            .execute()
        )
        return len(result.data or [])

    def restore_sms_contacts(self, contact_ids: Iterable[str]) -> int:
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        result = (
            self.client.table("sms_contacts")
            .update({"deleted_at": None, "updated_at": "now()"})
            .in_("id", ids)
            .execute()
        )
        return len(result.data or [])

    def touch_sms_contact_lifecycle(
        self, phone: str, status: str, when: str | None = None
    ) -> None:
        if not phone:
            return
        self.client.table("sms_contacts").update({
            "last_recipient_status": status,
            "last_event_at": when or "now()",
        }).eq("phone", str(phone).strip()).execute()

    def bulk_touch_sms_contact_lifecycle(self, phones: Iterable[str], status: str) -> None:
        cleaned = [str(p).strip() for p in phones if p and str(p).strip()]
        if not cleaned:
            return
        self.client.table("sms_contacts").update({
            "last_recipient_status": status,
            "last_event_at": "now()",
        }).in_("phone", cleaned).execute()

    def register_sms_soft_bounce(self, phone: str, threshold: int) -> bool:
        """Bump an SMS contact's soft-bounce tally; suppress at ``threshold``."""
        phone = str(phone).strip()
        rows = (
            self.client.table("sms_contacts")
            .select("id,soft_bounce_count")
            .eq("phone", phone)
            .execute()
            .data
            or []
        )
        if not rows:
            return False
        count = int(rows[0].get("soft_bounce_count") or 0) + 1
        suppressed = threshold > 0 and count >= threshold
        update: dict[str, Any] = {
            "soft_bounce_count": count,
            "last_soft_bounce_at": "now()",
        }
        if suppressed:
            update["status"] = "bounced_soft"
            update["safe_to_send"] = False
        self.client.table("sms_contacts").update(update).eq("phone", phone).execute()
        return suppressed

    def delete_sms_contacts(self, contact_ids: Iterable[str]) -> int:
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        result = self.client.table("sms_contacts").delete().in_("id", ids).execute()
        return len(result.data or [])

    def update_sms_contact(self, contact_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        payload = {k: v for k, v in data.items() if v is not None or k == "metadata"}
        if payload.get("phone") is not None:
            payload["phone"] = str(payload["phone"]).strip()
        payload["updated_at"] = "now()"
        row = (
            self.client.table("sms_contacts")
            .update(payload)
            .eq("id", contact_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def delete_sms_contact(self, contact_id: str) -> bool:
        result = (
            self.client.table("sms_contacts").delete().eq("id", contact_id).execute()
        )
        return bool(result.data)

    def search_sms_contacts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = (query or "").strip()
        base = self.client.table("sms_contacts").select(
            "id,phone,first_name,last_name,status"
        )
        if q:
            escaped = q.replace(",", " ")
            base = base.or_(
                f"phone.ilike.%{escaped}%,first_name.ilike.%{escaped}%,"
                f"last_name.ilike.%{escaped}%"
            )
        return base.order("created_at", desc=True).limit(limit).execute().data or []

    def ensure_sms_contacts(self, rows: Iterable[dict[str, Any]]) -> dict[str, str]:
        """Create any missing SMS contacts, returning a ``phone -> id`` map.

        Unknown phones are created (DB defaults make them ``active``); existing
        contacts keep their suppression / soft-delete state.
        """
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            phone = str(row.get("phone") or "").strip()
            if not phone or phone in seen:
                continue
            seen.add(phone)
            entry: dict[str, Any] = {"phone": phone}
            for field in ("country", "first_name", "last_name", "metadata"):
                if row.get(field) is not None:
                    entry[field] = row[field]
            prepared.append(entry)

        if not prepared:
            return {}
        data = (
            self.client.table("sms_contacts")
            .upsert(prepared, on_conflict="phone")
            .execute()
            .data
            or []
        )
        return {r["phone"]: r["id"] for r in data if r.get("phone") and r.get("id")}

    # ------------------------------------------------------------------ #
    # SMS: campaigns
    # ------------------------------------------------------------------ #
    def create_sms_campaign(self, data: dict[str, Any]) -> dict[str, Any]:
        row = self.client.table("sms_campaigns").insert(data).execute().data
        if not row:
            raise SupabaseClientError("sms_campaign insert returned no row")
        return row[0]

    def get_sms_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        data = (
            self.client.table("sms_campaigns").select("*").eq("id", campaign_id).execute().data
        )
        return data[0] if data else None

    def update_sms_campaign(
        self, campaign_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = (
            self.client.table("sms_campaigns")
            .update(data)
            .eq("id", campaign_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def delete_sms_campaign(self, campaign_id: str) -> bool:
        result = (
            self.client.table("sms_campaigns").delete().eq("id", campaign_id).execute()
        )
        return bool(result.data)

    def set_sms_priority(self, campaign_id: str, priority: str) -> dict[str, Any] | None:
        row = (
            self.client.table("sms_campaigns")
            .update({"priority": priority})
            .eq("id", campaign_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def _sms_contact_ids_for(self, rows: Iterable[dict[str, Any]]) -> list[str]:
        rows = list(rows)
        if not rows:
            return []
        return list(self.ensure_sms_contacts(rows).values())

    def add_sms_recipients(
        self, campaign_id: str, contact_rows: Iterable[dict[str, Any]]
    ) -> int:
        rows = [
            {"campaign_id": campaign_id, "contact_id": contact_id}
            for contact_id in self._sms_contact_ids_for(contact_rows)
        ]
        if not rows:
            return 0
        result = self.client.table("sms_recipients").upsert(
            rows, on_conflict="campaign_id,contact_id"
        ).execute()
        return len(result.data or [])

    def add_sms_recipient_ids(
        self, campaign_id: str, contact_ids: Iterable[str]
    ) -> int:
        rows = [
            {"campaign_id": campaign_id, "contact_id": cid} for cid in set(contact_ids)
        ]
        if not rows:
            return 0
        result = self.client.table("sms_recipients").upsert(
            rows, on_conflict="campaign_id,contact_id"
        ).execute()
        return len(result.data or [])

    def list_sms_recipients(
        self, campaign_id: str, status: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        query = self.client.table("sms_recipients").select("*").eq(
            "campaign_id", campaign_id
        ).limit(limit)
        if status:
            query = query.eq("status", status)
        return query.execute().data or []

    def find_sms_recipient_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        """Resolve the recipient row a Brevo SMS webhook refers to."""
        if not message_id:
            return None
        data = (
            self.client.table("sms_recipients")
            .select("*")
            .eq("message_id", str(message_id))
            .limit(1)
            .execute()
            .data
        )
        return data[0] if data else None

    def update_sms_recipient(self, recipient_id: str, data: dict[str, Any]) -> None:
        self.client.table("sms_recipients").update(data).eq("id", recipient_id).execute()

    def bulk_update_sms_recipients(
        self, recipient_ids: Iterable[str], data: dict[str, Any]
    ) -> None:
        ids = list(recipient_ids)
        if not ids:
            return
        self.client.table("sms_recipients").update(data).in_("id", ids).execute()

    def count_sms_recipients(self, campaign_id: str, status: str | None = None) -> int:
        query = self.client.table("sms_recipients").select("id", count="exact").eq(
            "campaign_id", campaign_id
        )
        if status:
            query = query.eq("status", status)
        return query.execute().count or 0

    def count_sms_sent_since(self, since_iso: str) -> int:
        """Count SMS sent at/after ``since_iso`` across all campaigns (caps)."""
        query = (
            self.client.table("sms_recipients")
            .select("id", count="exact")
            .gte("sent_at", since_iso)
        )
        return query.execute().count or 0

    def count_sms_credits_since(self, since_iso: str) -> int:
        """Sum credits billed at/after ``since_iso`` (daily credit ceiling)."""
        rows = (
            self.client.table("sms_recipients")
            .select("credits_used")
            .gte("sent_at", since_iso)
            .execute()
            .data
            or []
        )
        total = 0.0
        for row in rows:
            try:
                total += float(row.get("credits_used") or 0)
            except (TypeError, ValueError):
                continue
        return int(total)

    def reset_sms_recipients(
        self, campaign_id: str, contact_ids: Iterable[str] | None = None
    ) -> int:
        payload = {
            "status": "queued",
            "sent_at": None,
            "claimed_at": None,
            "delivered_at": None,
            "last_error": None,
            "message_id": None,
            "segments": None,
            "credits_used": None,
        }
        query = (
            self.client.table("sms_recipients")
            .update(payload)
            .eq("campaign_id", campaign_id)
        )
        if contact_ids is not None:
            ids = list(contact_ids)
            if not ids:
                return 0
            query = query.in_("contact_id", ids)
        result = query.execute()
        return len(result.data or [])

    def list_active_sms_campaigns(self) -> list[dict[str, Any]]:
        data = (
            self.client.table("sms_campaigns")
            .select("*")
            .in_("status", ["scheduled", "sending"])
            .execute()
            .data
        )
        return data or []

    def list_sending_sms_campaigns(self) -> list[dict[str, Any]]:
        data = (
            self.client.table("sms_campaigns")
            .select("*")
            .eq("status", "sending")
            .execute()
            .data
        )
        return data or []

    def list_held_sms_campaigns(self) -> list[dict[str, Any]]:
        data = (
            self.client.table("sms_campaigns")
            .select("*")
            .eq("status", "held")
            .execute()
            .data
        )
        return data or []

    def list_due_scheduled_sms(self) -> list[dict[str, Any]]:
        from app.core.scheduling import is_due

        rows = (
            self.client.table("sms_campaigns")
            .select("*")
            .eq("status", "scheduled")
            .execute()
            .data
            or []
        )
        return [row for row in rows if is_due(row.get("schedule_at"))]

    def list_engagement_sms_campaigns(self, hours: int = 24) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = (
            self.client.table("sms_campaigns")
            .select("*")
            .in_("status", ["sending", "completed"])
            .execute()
            .data
            or []
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") == "sending":
                out.append(row)
            elif str(row.get("finished_at") or "") >= since:
                out.append(row)
        return out

    def increment_sms_stats(self, campaign_id: str, field: str, delta: int = 1) -> None:
        campaign = self.get_sms_campaign(campaign_id)
        if campaign is None:
            return
        stats = dict(campaign.get("stats") or {})
        stats[field] = stats.get(field, 0) + delta
        self.client.table("sms_campaigns").update({"stats": stats}).eq(
            "id", campaign_id
        ).execute()

    def get_sms_campaign_metrics(self, campaign_id: str) -> dict[str, int | float]:
        campaigns = (
            self.client.table("sms_campaigns")
            .select("stats")
            .eq("id", campaign_id)
            .execute()
            .data
        )
        stats = (campaigns[0] or {}).get("stats", {}) if campaigns else {}
        return {
            "sent": stats.get("sent", 0),
            "delivered": stats.get("delivered", 0),
            "bounces": stats.get("bounces", 0),
            "skipped": stats.get("skipped", 0),
            "failed": stats.get("failed", 0),
            "credits_used": stats.get("credits_used", 0),
        }

    def thread_sms_root(self, campaign_id: str) -> str | None:
        campaign = self.get_sms_campaign(campaign_id)
        if campaign is None:
            return None
        return campaign.get("reuse_root_id") or campaign["id"]

    def thread_sms_campaign_ids(self, root_id: str) -> list[str]:
        rows = (
            self.client.table("sms_campaigns")
            .select("id")
            .eq("reuse_root_id", root_id)
            .execute()
            .data
            or []
        )
        ids = {r["id"] for r in rows}
        ids.add(root_id)
        return sorted(ids)

    def thread_sms_contact_ids(self, root_id: str) -> list[str]:
        campaign_ids = self.thread_sms_campaign_ids(root_id)
        if not campaign_ids:
            return []
        rows = (
            self.client.table("sms_recipients")
            .select("contact_id")
            .in_("campaign_id", campaign_ids)
            .execute()
            .data
            or []
        )
        return sorted({r["contact_id"] for r in rows})

    def sms_campaign_target_contact_ids(self, campaign_id: str) -> list[str]:
        lists = self.list_sms_campaign_lists(campaign_id)
        if lists:
            return sorted(self.sms_contact_ids_for_lists([l["id"] for l in lists]))
        data = self.list_sms_contacts(page=1, limit=100000)
        return sorted(c["id"] for c in data["items"] if c.get("status") == "active")

    # ------------------------------------------------------------------ #
    # SMS: stored procedures
    # ------------------------------------------------------------------ #
    def claim_sms_recipients(self, campaign_id: str, limit: int) -> list[dict[str, Any]]:
        data = self.client.rpc("claim_sms_recipients", {
            "p_campaign_id": campaign_id,
            "p_limit": limit,
        }).execute().data
        return data or []

    def suppress_sms_contact(self, phone: str, reason: str) -> None:
        self.client.rpc("suppress_sms_contact", {
            "p_phone": phone,
            "p_reason": reason,
        }).execute()

    def requeue_stale_sms_recipients(self, timeout_seconds: int = 900) -> int:
        result = self.client.rpc(
            "requeue_stale_sms_recipients", {"p_timeout_seconds": timeout_seconds}
        ).execute()
        return int(result.data or 0)

    # ------------------------------------------------------------------ #
    # SMS: events
    # ------------------------------------------------------------------ #
    def record_sms_event(
        self,
        event_type: str,
        campaign_id: str | None = None,
        recipient_id: str | None = None,
        raw_payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist an SMS delivery/webhook event; best-effort, never raising."""
        payload: dict[str, Any] = {
            "event_type": event_type,
            "raw_payload": raw_payload or {},
        }
        if campaign_id:
            payload["campaign_id"] = campaign_id
        if recipient_id:
            payload["recipient_id"] = recipient_id
        try:
            self.client.table("sms_events").insert(payload).execute()
            return
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("record_sms_event failed (%s); retrying without FKs", exc)
        anonymous = {
            k: v for k, v in payload.items() if k not in ("campaign_id", "recipient_id")
        }
        try:
            self.client.table("sms_events").insert(anonymous).execute()
        except Exception as exc:  # noqa: BLE001 - give up, but never raise
            logger.error("record_sms_event could not be stored: %s", exc)

    # ------------------------------------------------------------------ #
    # SMS: lists
    # ------------------------------------------------------------------ #
    def list_sms_lists(self) -> list[dict[str, Any]]:
        lists = (
            self.client.table("sms_lists").select("*").order("name").execute().data or []
        )
        members = (
            self.client.table("sms_list_members").select("list_id").execute().data or []
        )
        counts: dict[str, int] = {}
        for row in members:
            counts[row["list_id"]] = counts.get(row["list_id"], 0) + 1
        for entry in lists:
            entry["member_count"] = counts.get(entry["id"], 0)
        return lists

    def create_sms_list(self, name: str, description: str | None = None) -> dict[str, Any]:
        row = (
            self.client.table("sms_lists")
            .insert({"name": name, "description": description, "created_at": "now()"})
            .execute()
            .data
        )
        if not row:
            raise SupabaseClientError("sms_list insert returned no row")
        return row[0]

    def get_sms_list(self, list_id: str) -> dict[str, Any] | None:
        data = self.client.table("sms_lists").select("*").eq("id", list_id).execute().data
        return data[0] if data else None

    def update_sms_list(self, list_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        row = (
            self.client.table("sms_lists")
            .update({**data, "updated_at": "now()"})
            .eq("id", list_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def delete_sms_list(self, list_id: str) -> bool:
        result = self.client.table("sms_lists").delete().eq("id", list_id).execute()
        return bool(result.data)

    def list_sms_members(self, list_id: str) -> list[dict[str, Any]]:
        memberships = (
            self.client.table("sms_list_members")
            .select("contact_id,added_at")
            .eq("list_id", list_id)
            .execute()
            .data
            or []
        )
        ids = [m["contact_id"] for m in memberships]
        if not ids:
            return []
        contacts = (
            self.client.table("sms_contacts")
            .select("id,phone,first_name,last_name,status")
            .in_("id", ids)
            .execute()
            .data
            or []
        )
        by_id = {c["id"]: c for c in contacts}
        added = {m["contact_id"]: m.get("added_at") for m in memberships}
        return [
            {**by_id[cid], "added_at": added.get(cid)}
            for cid in ids
            if cid in by_id
        ]

    def add_sms_members(self, list_id: str, contact_ids: Iterable[str]) -> int:
        rows = [{"list_id": list_id, "contact_id": cid} for cid in set(contact_ids)]
        if not rows:
            return 0
        result = self.client.table("sms_list_members").upsert(
            rows, on_conflict="list_id,contact_id"
        ).execute()
        return len(result.data or [])

    def add_sms_members_by_phone(self, list_id: str, phones: Iterable[str]) -> int:
        rows = [{"phone": str(p).strip()} for p in phones if p and str(p).strip()]
        if not rows:
            return 0
        return self.add_sms_members(list_id, list(self.ensure_sms_contacts(rows).values()))

    def remove_sms_member(self, list_id: str, contact_id: str) -> bool:
        result = (
            self.client.table("sms_list_members")
            .delete()
            .eq("list_id", list_id)
            .eq("contact_id", contact_id)
            .execute()
        )
        return bool(result.data)

    def sms_contact_ids_for_lists(self, list_ids: Iterable[str]) -> list[str]:
        ids = list({lid for lid in list_ids if lid})
        if not ids:
            return []
        rows = (
            self.client.table("sms_list_members")
            .select("contact_id")
            .in_("list_id", ids)
            .execute()
            .data
            or []
        )
        return list({r["contact_id"] for r in rows})

    def add_sms_campaign_lists(self, campaign_id: str, list_ids: Iterable[str]) -> int:
        rows = [{"campaign_id": campaign_id, "list_id": lid} for lid in set(list_ids)]
        if not rows:
            return 0
        result = self.client.table("sms_campaign_lists").upsert(
            rows, on_conflict="campaign_id,list_id"
        ).execute()
        return len(result.data or [])

    def list_sms_campaign_lists(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = (
            self.client.table("sms_campaign_lists")
            .select("list_id")
            .eq("campaign_id", campaign_id)
            .execute()
            .data
            or []
        )
        ids = [r["list_id"] for r in rows]
        if not ids:
            return []
        return (
            self.client.table("sms_lists").select("*").in_("id", ids).execute().data or []
        )

    def replace_sms_campaign_lists(
        self, campaign_id: str, list_ids: Iterable[str]
    ) -> None:
        self.client.table("sms_campaign_lists").delete().eq(
            "campaign_id", campaign_id
        ).execute()
        self.add_sms_campaign_lists(campaign_id, list_ids)

    def remove_sms_campaign_list(self, campaign_id: str, list_id: str) -> bool:
        result = (
            self.client.table("sms_campaign_lists")
            .delete()
            .eq("campaign_id", campaign_id)
            .eq("list_id", list_id)
            .execute()
        )
        return bool(result.data)

    # ------------------------------------------------------------------ #
    # SMS: templates
    # ------------------------------------------------------------------ #
    def list_sms_templates(self) -> list[dict[str, Any]]:
        return (
            self.client.table("sms_templates")
            .select("*")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )

    def get_sms_template(self, template_id: str) -> dict[str, Any] | None:
        data = (
            self.client.table("sms_templates").select("*").eq("id", template_id).execute().data
        )
        return data[0] if data else None

    def create_sms_template(
        self,
        name: str,
        content: str,
        sms_type: str = "transactional",
        unicode_enabled: bool = False,
        organisation_prefix: str | None = None,
    ) -> dict[str, Any]:
        row = (
            self.client.table("sms_templates")
            .insert({
                "name": name,
                "content": content,
                "sms_type": sms_type,
                "unicode_enabled": unicode_enabled,
                "organisation_prefix": organisation_prefix,
            })
            .execute()
            .data
        )
        if not row:
            raise SupabaseClientError("sms_template insert returned no row")
        return row[0]

    def update_sms_template(
        self, template_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        row = (
            self.client.table("sms_templates")
            .update({**data, "updated_at": "now()"})
            .eq("id", template_id)
            .execute()
            .data
        )
        return row[0] if row else None

    def delete_sms_template(self, template_id: str) -> bool:
        result = (
            self.client.table("sms_templates").delete().eq("id", template_id).execute()
        )
        return bool(result.data)

    # ------------------------------------------------------------------ #
    # SMS: settings
    # ------------------------------------------------------------------ #
    def get_sms_settings_row(self) -> dict[str, Any] | None:
        data = self.client.table("sms_settings").select("*").limit(1).execute().data
        return data[0] if data else None

    def update_sms_settings(self, data: dict[str, Any]) -> dict[str, Any] | None:
        """Update the single sms_settings row, creating it if absent."""
        row = self.get_sms_settings_row()
        payload = {**data, "updated_at": "now()"}
        if row:
            updated = (
                self.client.table("sms_settings")
                .update(payload)
                .eq("id", row["id"])
                .execute()
                .data
            )
            return updated[0] if updated else None
        inserted = self.client.table("sms_settings").insert(payload).execute().data
        return inserted[0] if inserted else None

    def ping(self) -> bool:
        try:
            data = self.client.table("health_checks").select("*").limit(1).execute().data
            return True
        except Exception:
            return False


_singleton: SupabaseWrapper | None = None


def get_supabase() -> SupabaseWrapper:
    """Return a lazily initialised SupabaseWrapper singleton."""
    global _singleton
    if _singleton is None:
        _singleton = SupabaseWrapper()
    return _singleton


def verify_supabase_token(access_token: str) -> dict[str, Any]:
    """Validate a Supabase access token against the Auth API.

    Returns the user's ``{"id", "email", ...}`` payload on success and raises
    :class:`SupabaseClientError` for missing/invalid/expired tokens. Uses the
    anon key (falling back to the service-role key) as the ``apikey`` header.
    """
    import requests

    token = (access_token or "").strip()
    if not token:
        raise SupabaseClientError("missing access token")

    settings = get_settings()
    base = (settings.supabase_url or "").rstrip("/")
    api_key = settings.supabase_anon_key or settings.supabase_service_role_key
    if not base or not api_key:
        raise SupabaseClientError("Supabase URL / anon key is not configured")

    try:
        resp = requests.get(
            f"{base}/auth/v1/user",
            headers={"apikey": api_key, "Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except requests.RequestException as exc:  # pragma: no cover - network path
        raise SupabaseClientError(f"auth service unreachable: {exc}") from exc

    if resp.status_code != 200:
        raise SupabaseClientError("invalid or expired token")
    payload = resp.json() or {}
    if not payload.get("id"):
        raise SupabaseClientError("token did not resolve to a user")
    return payload


def reset_supabase() -> None:
    global _singleton
    _singleton = None
