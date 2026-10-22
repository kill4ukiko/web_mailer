"""Bird SMS persistence layer.

All Bird tables are isolated from the email and Brevo SMS stacks. The method
names intentionally mirror the SMS store so protocol code can be shared, but
every query resolves through ``_table`` to the ``bird_sms_*`` schema.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.core.supabase_client import SupabaseClientError, get_supabase


class _MappedClient:
    def __init__(self, client) -> None:
        self._client = client

    def table(self, name: str):
        mapped = name.replace("sms_", "bird_sms_", 1) if name.startswith("sms_") else name
        return self._client.table(mapped)

    def __getattr__(self, item):
        return getattr(self._client, item)


class BirdSmsStore:
    def __init__(self, supabase=None) -> None:
        self._supabase = supabase or get_supabase()
        self.client = _MappedClient(self._supabase.client)

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def get_settings_row(self) -> dict[str, Any] | None:
        data = self.client.table("bird_sms_settings").select("*").limit(1).execute().data
        return data[0] if data else None

    def update_settings(self, data: dict[str, Any]) -> dict[str, Any] | None:
        row = self.get_settings_row()
        payload = {**data, "updated_at": "now()"}
        if row:
            updated = (
                self.client.table("bird_sms_settings").update(payload)
                .eq("id", row["id"]).execute().data
            )
            return updated[0] if updated else None
        inserted = self.client.table("bird_sms_settings").insert(payload).execute().data
        return inserted[0] if inserted else None

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #
    @staticmethod
    def _profile(data: dict[str, Any]) -> dict[str, Any]:
        allowed = {"phone", "country", "first_name", "last_name", "metadata"}
        payload = {k: v for k, v in data.items() if k in allowed}
        payload["phone"] = str(data.get("phone") or "").strip()
        return payload

    def list_contacts(self, page: int = 1, limit: int = 50,
                      status: str | None = None, include_deleted: bool = False,
                      delivery: str | None = None) -> dict[str, Any]:
        query = self.client.table("bird_sms_contacts").select("*", count="exact")
        if not include_deleted:
            query = query.is_("deleted_at", "null")
        if status:
            query = query.eq("status", status)
        if delivery:
            query = query.eq("last_recipient_status", delivery)
        result = (
            query.order("created_at", desc=True)
            .range((page - 1) * limit, page * limit - 1)
            .execute()
        )
        return {"items": result.data or [], "total": result.count or 0,
                "page": page, "limit": limit}

    # SMS-compatible alias
    list_sms_contacts = list_contacts

    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_contacts").select("*").eq("id", contact_id).execute().data
        return rows[0] if rows else None

    def create_contact(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        return (
            self.client.table("bird_sms_contacts")
            .upsert(self._profile(data), on_conflict="phone").execute().data or []
        )

    create_sms_contact = create_contact

    def update_contact(self, contact_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        payload = {**data, "updated_at": "now()"}
        rows = self.client.table("bird_sms_contacts").update(payload).eq("id", contact_id).execute().data
        return rows[0] if rows else None

    update_sms_contact = update_contact

    def update_contact_status(self, phone: str, status: str) -> None:
        self.client.table("bird_sms_contacts").update({
            "status": status, "safe_to_send": status == "active", "updated_at": "now()",
        }).eq("phone", str(phone).strip()).execute()

    update_sms_contact_status = update_contact_status

    def bulk_insert_contacts(self, contacts: Iterable[dict[str, Any]]) -> tuple[int, list[str]]:
        rows = [self._profile(dict(c)) for c in contacts]
        if not rows:
            return 0, []
        data = self.client.table("bird_sms_contacts").upsert(
            rows, on_conflict="phone"
        ).execute().data or []
        return len(data), []

    bulk_insert_sms_contacts = bulk_insert_contacts

    def ensure_contacts(self, rows: Iterable[dict[str, Any]]) -> dict[str, str]:
        prepared = []
        seen = set()
        for row in rows:
            phone = str(row.get("phone") or "").strip()
            if not phone or phone in seen:
                continue
            seen.add(phone)
            prepared.append(self._profile({**row, "phone": phone}))
        if not prepared:
            return {}
        data = self.client.table("bird_sms_contacts").upsert(
            prepared, on_conflict="phone"
        ).execute().data or []
        return {str(r["phone"]): r["id"] for r in data if r.get("phone")}

    ensure_sms_contacts = ensure_contacts

    def delete_contact(self, contact_id: str) -> bool:
        result = self.client.table("bird_sms_contacts").delete().eq("id", contact_id).execute()
        return bool(result.data)

    delete_sms_contact = delete_contact

    def delete_contacts(self, ids: Iterable[str]) -> int:
        ids = [i for i in ids if i]
        if not ids:
            return 0
        return len(self.client.table("bird_sms_contacts").delete().in_("id", ids).execute().data or [])

    delete_sms_contacts = delete_contacts

    def soft_delete_contacts(self, ids: Iterable[str]) -> int:
        ids = [i for i in ids if i]
        if not ids:
            return 0
        rows = self.client.table("bird_sms_contacts").update(
            {"deleted_at": "now()", "updated_at": "now()"}
        ).in_("id", ids).execute().data or []
        return len(rows)

    soft_delete_sms_contacts = soft_delete_contacts

    def restore_contacts(self, ids: Iterable[str]) -> int:
        ids = [i for i in ids if i]
        if not ids:
            return 0
        rows = self.client.table("bird_sms_contacts").update(
            {"deleted_at": None, "updated_at": "now()"}
        ).in_("id", ids).execute().data or []
        return len(rows)

    restore_sms_contacts = restore_contacts

    def search_contacts(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        query = self.client.table("bird_sms_contacts").select("*").is_("deleted_at", "null")
        if q:
            # PostgREST OR syntax; phone/name search is intentionally simple.
            query = query.or_(f"phone.ilike.%{q}%,first_name.ilike.%{q}%,last_name.ilike.%{q}%")
        return query.limit(limit).execute().data or []

    search_sms_contacts = search_contacts

    # ------------------------------------------------------------------ #
    # Lists
    # ------------------------------------------------------------------ #
    def list_lists(self) -> list[dict[str, Any]]:
        lists = self.client.table("bird_sms_lists").select("*").order("name").execute().data or []
        members = self.client.table("bird_sms_list_members").select("list_id").execute().data or []
        counts: dict[str, int] = {}
        for row in members:
            counts[row["list_id"]] = counts.get(row["list_id"], 0) + 1
        for entry in lists:
            entry["member_count"] = counts.get(entry["id"], 0)
        return lists

    list_sms_lists = list_lists

    def get_list(self, list_id: str) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_lists").select("*").eq("id", list_id).execute().data
        return rows[0] if rows else None

    get_sms_list = get_list

    def create_list(self, name: str, description: str | None = None):
        try:
            rows = self.client.table("bird_sms_lists").insert(
                {"name": name, "description": description}
            ).execute().data
        except Exception as exc:  # noqa: BLE001
            raise SupabaseClientError(str(exc)) from exc
        return rows[0] if rows else None

    create_sms_list = create_list

    def update_list(self, list_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_lists").update(
            {**data, "updated_at": "now()"}
        ).eq("id", list_id).execute().data
        return rows[0] if rows else None

    update_sms_list = update_list

    def delete_list(self, list_id: str) -> bool:
        return bool(self.client.table("bird_sms_lists").delete().eq("id", list_id).execute().data)

    delete_sms_list = delete_list

    def add_members(self, list_id: str, contact_ids: Iterable[str]) -> int:
        rows = [{"list_id": list_id, "contact_id": cid} for cid in set(contact_ids) if cid]
        if not rows:
            return 0
        return len(self.client.table("bird_sms_list_members").upsert(
            rows, on_conflict="list_id,contact_id"
        ).execute().data or [])

    add_sms_members = add_members

    def add_members_by_phone(self, list_id: str, phones: Iterable[str]) -> int:
        rows = [{"phone": str(p).strip()} for p in phones if p and str(p).strip()]
        if not rows:
            return 0
        return self.add_members(list_id, self.ensure_contacts(rows).values())

    add_sms_members_by_phone = add_members_by_phone

    def list_members(self, list_id: str) -> list[dict[str, Any]]:
        links = self.client.table("bird_sms_list_members").select("contact_id").eq(
            "list_id", list_id
        ).execute().data or []
        ids = [r["contact_id"] for r in links]
        if not ids:
            return []
        return self.client.table("bird_sms_contacts").select("*").in_("id", ids).execute().data or []

    list_sms_members = list_members

    def remove_member(self, list_id: str, contact_id: str) -> bool:
        return bool(self.client.table("bird_sms_list_members").delete().eq(
            "list_id", list_id
        ).eq("contact_id", contact_id).execute().data)

    remove_sms_member = remove_member

    def contact_ids_for_lists(self, list_ids: Iterable[str]) -> list[str]:
        ids = [lid for lid in list_ids if lid]
        if not ids:
            return []
        rows = self.client.table("bird_sms_list_members").select(
            "contact_id"
        ).in_("list_id", ids).execute().data or []
        return list({r["contact_id"] for r in rows})

    sms_contact_ids_for_lists = contact_ids_for_lists

    # ------------------------------------------------------------------ #
    # Templates
    # ------------------------------------------------------------------ #
    def list_templates(self) -> list[dict[str, Any]]:
        return self.client.table("bird_sms_templates").select("*").order(
            "created_at", desc=True
        ).execute().data or []

    list_sms_templates = list_templates

    def get_template(self, template_id: str) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_templates").select("*").eq("id", template_id).execute().data
        return rows[0] if rows else None

    get_sms_template = get_template

    def create_template(self, name: str, content: str, category: str = "transactional",
                        include_stop_code: bool = False):
        try:
            rows = self.client.table("bird_sms_templates").insert({
                "name": name, "content": content, "category": category,
                "include_stop_code": include_stop_code,
            }).execute().data
        except Exception as exc:  # noqa: BLE001
            raise SupabaseClientError(str(exc)) from exc
        return rows[0] if rows else None

    create_sms_template = create_template

    def update_template(self, template_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_templates").update(
            {**data, "updated_at": "now()"}
        ).eq("id", template_id).execute().data
        return rows[0] if rows else None

    update_sms_template = update_template

    def delete_template(self, template_id: str) -> bool:
        return bool(self.client.table("bird_sms_templates").delete().eq("id", template_id).execute().data)

    delete_sms_template = delete_template

    # ------------------------------------------------------------------ #
    # Campaigns
    # ------------------------------------------------------------------ #
    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_campaigns").select("*").eq("id", campaign_id).execute().data
        return rows[0] if rows else None

    get_sms_campaign = get_campaign

    def create_campaign(self, data: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_campaigns").insert(data).execute().data
        return rows[0] if rows else None

    create_sms_campaign = create_campaign

    def update_campaign(self, campaign_id: str, data: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.client.table("bird_sms_campaigns").update(data).eq("id", campaign_id).execute().data
        return rows[0] if rows else None

    update_sms_campaign = update_campaign

    def delete_campaign(self, campaign_id: str) -> bool:
        return bool(self.client.table("bird_sms_campaigns").delete().eq("id", campaign_id).execute().data)

    delete_sms_campaign = delete_campaign

    def set_priority(self, campaign_id: str, priority: str) -> dict[str, Any] | None:
        return self.update_campaign(campaign_id, {"priority": priority})

    set_sms_priority = set_priority

    def list_campaigns(self, status: str | None = None) -> list[dict[str, Any]]:
        query = self.client.table("bird_sms_campaigns").select("*").order("created_at", desc=True).limit(100)
        if status:
            query = query.eq("status", status)
        return query.execute().data or []

    def list_sending_campaigns(self) -> list[dict[str, Any]]:
        return self.client.table("bird_sms_campaigns").select("*").eq("status", "sending").execute().data or []

    list_sending_sms_campaigns = list_sending_campaigns

    def list_active_campaigns(self) -> list[dict[str, Any]]:
        return self.client.table("bird_sms_campaigns").select("*").in_(
            "status", ["scheduled", "sending"]
        ).execute().data or []

    list_active_sms_campaigns = list_active_campaigns

    def list_held_campaigns(self) -> list[dict[str, Any]]:
        return self.client.table("bird_sms_campaigns").select("*").eq("status", "held").execute().data or []

    list_held_sms_campaigns = list_held_campaigns

    def list_due_scheduled(self) -> list[dict[str, Any]]:
        from app.core.scheduling import is_due
        rows = self.client.table("bird_sms_campaigns").select("*").eq("status", "scheduled").execute().data or []
        return [c for c in rows if is_due(c.get("schedule_at"))]

    list_due_scheduled_sms = list_due_scheduled

    # ------------------------------------------------------------------ #
    # Campaign lists / recipients
    # ------------------------------------------------------------------ #
    def add_campaign_lists(self, campaign_id: str, list_ids: Iterable[str]) -> int:
        rows = [{"campaign_id": campaign_id, "list_id": lid} for lid in set(list_ids) if lid]
        if not rows:
            return 0
        return len(self.client.table("bird_sms_campaign_lists").upsert(
            rows, on_conflict="campaign_id,list_id"
        ).execute().data or [])

    add_sms_campaign_lists = add_campaign_lists

    def replace_campaign_lists(self, campaign_id: str, list_ids: Iterable[str]) -> None:
        self.client.table("bird_sms_campaign_lists").delete().eq("campaign_id", campaign_id).execute()
        self.add_campaign_lists(campaign_id, list_ids)

    replace_sms_campaign_lists = replace_campaign_lists

    def list_campaign_lists(self, campaign_id: str) -> list[dict[str, Any]]:
        links = self.client.table("bird_sms_campaign_lists").select("list_id").eq(
            "campaign_id", campaign_id
        ).execute().data or []
        ids = [r["list_id"] for r in links]
        if not ids:
            return []
        return self.client.table("bird_sms_lists").select("*").in_("id", ids).execute().data or []

    list_sms_campaign_lists = list_campaign_lists

    def add_recipients(self, campaign_id: str, contact_rows: Iterable[dict[str, Any]]) -> int:
        phones = [{"phone": str(r.get("phone") or "").strip()} for r in contact_rows if r.get("phone")]
        if not phones:
            return 0
        ids = list(self.ensure_contacts(phones).values())
        return self.add_recipient_ids(campaign_id, ids)

    add_sms_recipients = add_recipients

    def add_recipient_ids(self, campaign_id: str, contact_ids: Iterable[str]) -> int:
        rows = [{"campaign_id": campaign_id, "contact_id": cid} for cid in set(contact_ids) if cid]
        if not rows:
            return 0
        return len(self.client.table("bird_sms_recipients").upsert(
            rows, on_conflict="campaign_id,contact_id"
        ).execute().data or [])

    add_sms_recipient_ids = add_recipient_ids

    def count_recipients(self, campaign_id: str) -> int:
        result = self.client.table("bird_sms_recipients").select(
            "id", count="exact"
        ).eq("campaign_id", campaign_id).execute()
        return result.count or 0

    count_sms_recipients = count_recipients

    def list_recipients(self, campaign_id: str, status: str | None = None,
                        limit: int = 1000) -> list[dict[str, Any]]:
        query = self.client.table("bird_sms_recipients").select("*").eq(
            "campaign_id", campaign_id
        ).limit(limit)
        if status:
            query = query.eq("status", status)
        return query.execute().data or []

    list_sms_recipients = list_recipients

    def claim_recipients(self, campaign_id: str, limit: int) -> list[dict[str, Any]]:
        return self.client.rpc("claim_bird_sms_recipients", {
            "p_campaign_id": campaign_id, "p_limit": limit,
        }).execute().data or []

    claim_sms_recipients = claim_recipients

    def update_recipient(self, recipient_id: str, data: dict[str, Any]) -> None:
        self.client.table("bird_sms_recipients").update(data).eq("id", recipient_id).execute()

    update_sms_recipient = update_recipient

    def bulk_update_recipients(self, ids: Iterable[str], data: dict[str, Any]) -> None:
        ids = [i for i in ids if i]
        if ids:
            self.client.table("bird_sms_recipients").update(data).in_("id", ids).execute()

    bulk_update_sms_recipients = bulk_update_recipients

    def find_recipient_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        if not message_id:
            return None
        rows = self.client.table("bird_sms_recipients").select("*").eq(
            "message_id", str(message_id)
        ).limit(1).execute().data or []
        return rows[0] if rows else None

    find_sms_recipient_by_message_id = find_recipient_by_message_id

    def reset_recipients(self, campaign_id: str, contact_ids: Iterable[str] | None = None) -> int:
        payload = {"status": "queued", "sent_at": None, "claimed_at": None,
                   "delivered_at": None, "last_error": None, "message_id": None,
                   "segments": None, "cost": None, "error_code": None,
                   "error_description": None, "carrier_error_code": None}
        query = self.client.table("bird_sms_recipients").update(payload).eq("campaign_id", campaign_id)
        if contact_ids is not None:
            ids = list(contact_ids)
            if not ids:
                return 0
            query = query.in_("contact_id", ids)
        return len(query.execute().data or [])

    reset_sms_recipients = reset_recipients

    def campaign_target_contact_ids(self, campaign_id: str) -> list[str]:
        lists = self.list_campaign_lists(campaign_id)
        if lists:
            return self.contact_ids_for_lists([l["id"] for l in lists])
        data = self.list_contacts(page=1, limit=100000)
        return [c["id"] for c in data["items"] if c.get("status") == "active"]

    sms_campaign_target_contact_ids = campaign_target_contact_ids

    def thread_root(self, campaign_id: str) -> str | None:
        campaign = self.get_campaign(campaign_id)
        return (campaign.get("reuse_root_id") or campaign["id"]) if campaign else None

    thread_sms_root = thread_root

    def thread_campaign_ids(self, root_id: str) -> list[str]:
        rows = self.client.table("bird_sms_campaigns").select("id").eq(
            "reuse_root_id", root_id
        ).execute().data or []
        return sorted({r["id"] for r in rows} | {root_id})

    thread_sms_campaign_ids = thread_campaign_ids

    def thread_contact_ids(self, root_id: str) -> list[str]:
        ids = self.thread_campaign_ids(root_id)
        rows = self.client.table("bird_sms_recipients").select(
            "contact_id"
        ).in_("campaign_id", ids).execute().data or []
        return sorted({r["contact_id"] for r in rows})

    thread_sms_contact_ids = thread_contact_ids

    # ------------------------------------------------------------------ #
    # Stats / events / budget counters
    # ------------------------------------------------------------------ #
    def increment_stats(self, campaign_id: str, field: str, delta: int | float = 1) -> None:
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            return
        stats = dict(campaign.get("stats") or {})
        stats[field] = stats.get(field, 0) + delta
        self.update_campaign(campaign_id, {"stats": stats})

    increment_sms_stats = increment_stats

    def get_campaign_metrics(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id) or {}
        return dict(campaign.get("stats") or {})

    get_sms_campaign_metrics = get_campaign_metrics

    def record_event(self, *, event_type: str, campaign_id: str | None = None,
                     recipient_id: str | None = None, webhook_id: str | None = None,
                     message_id: str | None = None, cost: float | None = None,
                     raw_payload: dict[str, Any] | None = None,
                     occurred_at: str | None = None) -> bool:
        payload = {
            "event_type": event_type, "campaign_id": campaign_id,
            "recipient_id": recipient_id, "webhook_id": webhook_id,
            "message_id": message_id, "cost": cost,
            "raw_payload": raw_payload or {},
        }
        if occurred_at:
            payload["occurred_at"] = occurred_at
        try:
            self.client.table("bird_sms_events").insert(payload).execute()
            return True
        except Exception:  # noqa: BLE001 - FK may be stale; retry anonymously
            try:
                payload.pop("campaign_id", None)
                payload.pop("recipient_id", None)
                self.client.table("bird_sms_events").insert(payload).execute()
                return True
            except Exception:  # noqa: BLE001
                return False

    def webhook_seen(self, webhook_id: str) -> bool:
        if not webhook_id:
            return False
        rows = self.client.table("bird_sms_events").select("id").eq(
            "webhook_id", webhook_id
        ).limit(1).execute().data or []
        return bool(rows)

    def count_sent_since(self, since_iso: str) -> int:
        query = self.client.table("bird_sms_recipients").select(
            "id", count="exact"
        ).gte("sent_at", since_iso)
        return query.execute().count or 0

    count_sms_sent_since = count_sent_since

    def sum_cost_since(self, since_iso: str) -> float:
        rows = self.client.table("bird_sms_recipients").select(
            "cost"
        ).gte("sent_at", since_iso).execute().data or []
        total = 0.0
        for row in rows:
            try:
                total += float(row.get("cost") or 0)
            except (TypeError, ValueError):
                pass
        return round(total, 4)

    count_sms_credits_since = sum_cost_since

    def bulk_touch_contact_lifecycle(self, phones: Iterable[str], status: str) -> None:
        cleaned = [str(p).strip() for p in phones if p and str(p).strip()]
        if cleaned:
            self.client.table("bird_sms_contacts").update({
                "last_recipient_status": status, "last_event_at": "now()",
            }).in_("phone", cleaned).execute()

    bulk_touch_sms_contact_lifecycle = bulk_touch_contact_lifecycle

    def list_engagement_campaigns(self, hours: int = 24) -> list[dict[str, Any]]:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.client.table("bird_sms_campaigns").select("*").in_(
            "status", ["sending", "completed"]
        ).execute().data or []
        return [c for c in rows if c["status"] == "sending" or str(c.get("finished_at") or "") >= since]

    list_engagement_sms_campaigns = list_engagement_campaigns


_store: BirdSmsStore | None = None


def get_bird_store() -> BirdSmsStore:
    global _store
    if _store is None or _store._supabase is not get_supabase():
        _store = BirdSmsStore()
    return _store


def reset_store() -> None:
    global _store
    _store = None

# Settings aliases used by the shared Telegram resolver.
BirdSmsStore.get_bird_sms_settings_row = BirdSmsStore.get_settings_row
BirdSmsStore.update_bird_sms_settings = BirdSmsStore.update_settings
