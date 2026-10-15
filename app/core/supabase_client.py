"""Supabase client wrapper and typed helper methods.

All read/write operations go through the PostgREST client using the
service-role key so the backend can bypass Row Level Security. Stored
procedures (claim_recipients / suppress_contact) are invoked via RPC.
"""
from __future__ import annotations

from typing import Any, Iterable

from supabase import Client, create_client

from app.config import Settings, get_settings


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
    def list_contacts(
        self, page: int = 1, limit: int = 50, status: str | None = None
    ) -> dict[str, Any]:
        query = self.client.table("contacts").select("*").range(
            (page - 1) * limit, page * limit - 1
        ).order("created_at", desc=True)
        if status:
            query = query.eq("status", status)
        data = query.execute().data
        total_resp = (
            self.client.table("contacts").select("id", count="exact")
            if not status
            else self.client.table("contacts").select("id", count="exact").eq("status", status)
        )
        total = total_resp.execute().count or 0
        return {"page": page, "limit": limit, "total": total, "items": data or []}

    def create_contact(self, data: dict[str, Any]) -> dict[str, Any] | None:
        email = str(data["email"]).lower().strip()
        payload = {**data, "email": email}
        return self.client.table("contacts").upsert(payload, on_conflict="email").execute().data

    def bulk_insert_contacts(self, contacts: Iterable[dict[str, Any]]) -> tuple[int, list[str]]:
        """Upsert a batch of contacts keyed on email.

        Returns (imported_count, list_of_invalid_email_rows).
        """
        rows = [dict(c) for c in contacts]
        if not rows:
            return 0, []
        imported = self.client.table("contacts").upsert(
            rows, on_conflict="email"
        ).execute().data
        return len(imported or []), []

    def update_contact_status(self, email: str, status: str) -> None:
        self.client.table("contacts").update(
            {"status": status, "updated_at": "now()"}
        ).eq("email", email.lower().strip()).execute()

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

        Unknown emails are created as ``active`` contacts so that list imports,
        manual entries and campaign extra-recipients never silently drop a row.
        """
        prepared: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            email = str(row.get("email") or "").lower().strip()
            if not email or email in seen:
                continue
            seen.add(email)
            entry: dict[str, Any] = {"email": email, "status": "active"}
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

    def update_recipient(self, recipient_id: str, data: dict[str, Any]) -> None:
        self.client.table("campaign_recipients").update(data).eq("id", recipient_id).execute()

    def bulk_update_recipients(self, recipient_ids: Iterable[str], data: dict[str, Any]) -> None:
        ids = list(recipient_ids)
        if not ids:
            return
        self.client.table("campaign_recipients").update(data).in_("id", ids).execute()

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
        self.client.table("email_events").insert(payload).execute()

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

    def increment_stats(self, campaign_id: str, field: str, delta: int = 1) -> None:
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