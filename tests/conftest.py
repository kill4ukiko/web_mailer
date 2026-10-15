"""PyTest fixtures. Runs against an in-memory fake Supabase (no network)."""
from __future__ import annotations

from typing import Any

import pytest

import app.core.supabase_client as supabase_module
from app.core.supabase_client import SupabaseClientError, SupabaseWrapper


class FakeClientTable:
    def __init__(self, store: list[dict]):
        self._store = store
        self._count = None
        self._filters: list[tuple] = []
        self._count_requested = False
        self._delete = False

    def select(self, *cols, count=None):
        if count:
            self._count_requested = True
        return self

    def range(self, a, b):
        return self

    def order(self, col, desc=False):
        return self

    def limit(self, n):
        return self

    def eq(self, col, val=None, **k):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def or_(self, expr):
        return self

    def _rows(self) -> list[dict]:
        rows = self._store
        for kind, col, val in self._filters:
            if kind == "eq":
                rows = [r for r in rows if r.get(col) == val]
            elif kind == "in":
                rows = [r for r in rows if r.get(col) in val]
            elif kind == "is":
                if val == "null":
                    rows = [r for r in rows if r.get(col) is None]
                else:
                    rows = [r for r in rows if r.get(col) == val]
        return rows

    def execute(self):
        if self._delete:
            rows = self._rows()
            for row in rows:
                self._store.remove(row)

            class D:
                data = rows
                count = len(rows)

            return D()

        rows = self._rows()

        class R:
            data = rows
            count = len(rows)

        return R()

    def delete(self):
        self._delete = True
        return self

    def update(self, data):
        for row in self._rows():
            row.update(data)
        return self

    @property
    def _match_ids(self) -> list[str]:
        return [row["id"] for row in self._store]

    def insert(self, data):
        return FakeInsertResponse(data)

    def upsert(self, data, on_conflict=None):
        store = self._store
        for row in data:
            key_email = row["email"].lower()
            existing = next((r for r in store if r.get("email") == key_email), None)
            if existing:
                existing.update(row)
            else:
                store.append(row)
        return FakeInsertResponse(data)


class FakeInsertResponse:
    def __init__(self, data):
        self.data = data

    def execute(self):
        class R:
            data = self.data
            count = len(self.data)
        return R()


class FakeSupabase:
    """In-memory stand-in for SupabaseWrapper used across tests."""

    def __init__(self):
        self.contacts: list[dict[str, Any]] = []
        self.campaigns: list[dict[str, Any]] = []
        self.recipients: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.settings_row: dict[str, Any] | None = None
        self.lists: list[dict[str, Any]] = []
        self.list_memberships: list[dict[str, Any]] = []
        self.campaign_lists: list[dict[str, Any]] = []
        self.templates: list[dict[str, Any]] = []
        self.recipient_cursor = 0
        self.campaign_cursor = 0
        self.contact_cursor = 0
        self.list_cursor = 0
        self.template_cursor = 0

    # ----- client-table shim (used directly by some routes) -----
    class _Client:
        def __init__(self, supabase):
            self._s = supabase

        def table(self, name):
            if name == "contacts":
                return FakeClientTable(self._s.contacts)
            if name == "campaigns":
                return FakeClientTable(self._s.campaigns)
            if name == "campaign_recipients":
                return FakeClientTable(self._s.recipients)
            if name == "email_events":
                return FakeClientTable(self._s.events)
            if name == "mailer_settings":
                return FakeClientTable([self._s.settings_row] if self._s.settings_row else [])
            if name == "contact_lists":
                return FakeClientTable(self._s.lists)
            if name == "contact_list_members":
                return FakeClientTable(self._s.list_memberships)
            if name == "campaign_lists":
                return FakeClientTable(self._s.campaign_lists)
            if name == "email_templates":
                return FakeClientTable(self._s.templates)
            raise KeyError(name)

        def rpc(self, name, params=None):
            s = self._s
            params = params or {}

            class R:
                data = 0

            if name == "requeue_stale_recipients":
                R.data = s.requeue_stale_recipients(params.get("p_timeout_seconds", 900))
            elif name == "claim_recipients":
                R.data = s.claim_recipients(params.get("p_campaign_id"), params.get("p_limit"))
            return R()

    @property
    def client(self):
        return self._Client(self)

    # ----- contacts -----
    def list_contacts(self, page=1, limit=50, status=None):
        items = [c for c in self.contacts if (status is None or c["status"] == status)]
        start = (page - 1) * limit
        return {"page": page, "limit": limit, "total": len(items), "items": items[start : start + limit]}

    def create_contact(self, data):
        email = data["email"].lower().strip()
        for c in self.contacts:
            if c["email"] == email:
                return [c]
        self.contact_cursor += 1
        row = {
            "id": f"c{self.contact_cursor}",
            "email": email,
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "status": "active",
            "timezone": data.get("timezone", "UTC"),
            "safe_to_send": True,
            "metadata": data.get("metadata", {}),
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.contacts.append(row)
        return [row]

    def bulk_insert_contacts(self, contacts):
        count = 0
        for row in contacts:
            result = self.create_contact({k: v for k, v in row.items() if k in ("email", "first_name", "last_name", "timezone", "metadata")})
            if result:
                count += 1
        return count, []

    def update_contact_status(self, email, status):
        for c in self.contacts:
            if c["email"] == email.lower().strip():
                c["status"] = status
                c["safe_to_send"] = status == "active"
                c["updated_at"] = "2026-01-01T00:00:00Z"

    def delete_contact(self, contact_id):
        before = len(self.contacts)
        self.contacts[:] = [c for c in self.contacts if c["id"] != contact_id]
        if len(self.contacts) == before:
            return False
        self.recipients[:] = [r for r in self.recipients if r["contact_id"] != contact_id]
        return True

    def delete_contacts(self, contact_ids):
        ids = [cid for cid in contact_ids if cid]
        if not ids:
            return 0
        before = len(self.contacts)
        self.contacts[:] = [c for c in self.contacts if c["id"] not in ids]
        self.recipients[:] = [r for r in self.recipients if r["contact_id"] not in ids]
        self.list_memberships[:] = [
            m for m in self.list_memberships if m["contact_id"] not in ids
        ]
        return before - len(self.contacts)

    def update_contact(self, contact_id, data):
        contact = next((c for c in self.contacts if c["id"] == contact_id), None)
        if contact is None:
            return None
        payload = {k: v for k, v in data.items() if v is not None or k == "metadata"}
        if "email" in payload and payload["email"] is not None:
            payload["email"] = str(payload["email"]).lower().strip()
        contact.update(payload)
        contact["updated_at"] = "2026-01-01T00:00:00Z"
        return contact

    def search_contacts(self, query, limit=20):
        q = (query or "").strip().lower()
        matches = [
            c for c in self.contacts
            if not q
            or q in c["email"]
            or q in (c.get("first_name") or "").lower()
            or q in (c.get("last_name") or "").lower()
        ]
        return [
            {
                "id": c["id"],
                "email": c["email"],
                "first_name": c.get("first_name"),
                "last_name": c.get("last_name"),
                "status": c["status"],
            }
            for c in matches[:limit]
        ]

    def ensure_contacts(self, rows):
        mapping = {}
        for row in rows:
            email = str(row.get("email") or "").lower().strip()
            if not email:
                continue
            contact = self._contact_by_email(email)
            if contact is None:
                contact = self.create_contact({
                    "email": email,
                    "first_name": row.get("first_name"),
                    "last_name": row.get("last_name"),
                    "timezone": row.get("timezone") or "UTC",
                    "metadata": row.get("metadata") or {},
                })[0]
            mapping[email] = contact["id"]
        return mapping

    # ----- contact lists -----
    def _contact_by_email(self, email):
        email = email.lower().strip()
        return next((c for c in self.contacts if c["email"] == email), None)

    def list_lists(self):
        out = []
        for entry in self.lists:
            count = sum(1 for m in self.list_memberships if m["list_id"] == entry["id"])
            out.append({**entry, "member_count": count})
        return out

    def create_list(self, name, description=None):
        self.list_cursor += 1
        row = {
            "id": f"list{self.list_cursor}",
            "name": name,
            "description": description,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        self.lists.append(row)
        return row

    def get_list(self, list_id):
        return next((entry for entry in self.lists if entry["id"] == list_id), None)

    def update_list(self, list_id, data):
        entry = self.get_list(list_id)
        if entry:
            entry.update(data)
        return entry

    def delete_list(self, list_id):
        before = len(self.lists)
        self.lists[:] = [entry for entry in self.lists if entry["id"] != list_id]
        self.list_memberships[:] = [m for m in self.list_memberships if m["list_id"] != list_id]
        self.campaign_lists[:] = [c for c in self.campaign_lists if c["list_id"] != list_id]
        return len(self.lists) != before

    def list_members(self, list_id):
        members = []
        for m in self.list_memberships:
            if m["list_id"] != list_id:
                continue
            contact = next((c for c in self.contacts if c["id"] == m["contact_id"]), None)
            if contact:
                members.append({
                    "id": contact["id"],
                    "email": contact["email"],
                    "first_name": contact.get("first_name"),
                    "last_name": contact.get("last_name"),
                    "status": contact["status"],
                    "added_at": m.get("added_at"),
                })
        return members

    def add_members(self, list_id, contact_ids):
        added = 0
        for cid in set(contact_ids):
            if any(m["list_id"] == list_id and m["contact_id"] == cid for m in self.list_memberships):
                continue
            self.list_memberships.append({"list_id": list_id, "contact_id": cid,
                                      "added_at": "2026-01-01T00:00:00Z"})
            added += 1
        return added

    def add_members_by_email(self, list_id, emails):
        rows = [{"email": str(e)} for e in emails if e and str(e).strip()]
        if not rows:
            return 0
        return self.add_members(list_id, list(self.ensure_contacts(rows).values()))

    def remove_member(self, list_id, contact_id):
        before = len(self.list_memberships)
        self.list_memberships[:] = [
            m for m in self.list_memberships
            if not (m["list_id"] == list_id and m["contact_id"] == contact_id)
        ]
        return len(self.list_memberships) != before

    def contact_ids_for_lists(self, list_ids):
        ids = {lid for lid in list_ids if lid}
        return list({
            m["contact_id"] for m in self.list_memberships if m["list_id"] in ids
        })

    def add_campaign_lists(self, campaign_id, list_ids):
        added = 0
        for lid in set(list_ids):
            if any(c["campaign_id"] == campaign_id and c["list_id"] == lid
                   for c in self.campaign_lists):
                continue
            self.campaign_lists.append({"campaign_id": campaign_id, "list_id": lid})
            added += 1
        return added

    def list_campaign_lists(self, campaign_id):
        ids = {c["list_id"] for c in self.campaign_lists if c["campaign_id"] == campaign_id}
        return [entry for entry in self.lists if entry["id"] in ids]

    def replace_campaign_lists(self, campaign_id, list_ids):
        self.campaign_lists[:] = [
            c for c in self.campaign_lists if c["campaign_id"] != campaign_id
        ]
        self.add_campaign_lists(campaign_id, list_ids)

    def remove_campaign_list(self, campaign_id, list_id):
        before = len(self.campaign_lists)
        self.campaign_lists[:] = [
            c for c in self.campaign_lists
            if not (c["campaign_id"] == campaign_id and c["list_id"] == list_id)
        ]
        return len(self.campaign_lists) != before

    def add_recipient_ids(self, campaign_id, contact_ids):
        added = 0
        for cid in set(contact_ids):
            if any(r["campaign_id"] == campaign_id and r["contact_id"] == cid
                   for r in self.recipients):
                continue
            self.recipient_cursor += 1
            self.recipients.append({
                "id": f"r{self.recipient_cursor}",
                "campaign_id": campaign_id,
                "contact_id": cid,
                "status": "queued",
                "sent_at": None,
                "opened_at": None,
                "clicked_at": None,
                "claimed_at": None,
                "last_error": None,
            })
            added += 1
        return added

    # ----- campaign reuse lineage -----
    def thread_root(self, campaign_id):
        campaign = self.get_campaign(campaign_id)
        if campaign is None:
            return None
        return campaign.get("reuse_root_id") or campaign["id"]

    def thread_campaign_ids(self, root_id):
        ids = {c["id"] for c in self.campaigns if c.get("reuse_root_id") == root_id}
        ids.add(root_id)
        return sorted(ids)

    def thread_contact_ids(self, root_id):
        ids = set(self.thread_campaign_ids(root_id))
        return sorted({
            r["contact_id"] for r in self.recipients if r["campaign_id"] in ids
        })

    def campaign_target_contact_ids(self, campaign_id):
        lists = self.list_campaign_lists(campaign_id)
        if lists:
            return sorted(self.contact_ids_for_lists([l["id"] for l in lists]))
        return sorted(c["id"] for c in self.contacts if c.get("status") == "active")

    def reset_recipients(self, campaign_id, contact_ids=None):
        wanted = set(contact_ids) if contact_ids is not None else None
        count = 0
        for r in self.recipients:
            if r["campaign_id"] != campaign_id:
                continue
            if wanted is not None and r["contact_id"] not in wanted:
                continue
            r.update({
                "status": "queued",
                "sent_at": None,
                "claimed_at": None,
                "opened_at": None,
                "clicked_at": None,
                "last_error": None,
            })
            count += 1
        return count

    def clear_duplicate_last_names(self):
        cleaned = 0
        for c in self.contacts:
            first = (c.get("first_name") or "").strip().lower()
            last = (c.get("last_name") or "").strip().lower()
            if first and last and first == last:
                c["last_name"] = None
                cleaned += 1
        return cleaned

    # ----- email templates -----
    def list_templates(self):
        return sorted(self.templates, key=lambda t: t.get("updated_at", ""), reverse=True)

    def get_template(self, template_id):
        return next((t for t in self.templates if t["id"] == template_id), None)

    def create_template(self, name, subject, html_content, plain_text_content=None):
        self.template_cursor += 1
        row = {
            "id": f"tpl{self.template_cursor}",
            "name": name,
            "subject": subject,
            "html_content": html_content,
            "plain_text_content": plain_text_content,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        self.templates.append(row)
        return row

    def update_template(self, template_id, data):
        entry = self.get_template(template_id)
        if entry:
            entry.update(data)
            entry["updated_at"] = "2026-01-02T00:00:00Z"
        return entry

    def delete_template(self, template_id):
        before = len(self.templates)
        self.templates[:] = [t for t in self.templates if t["id"] != template_id]
        return len(self.templates) != before
    def create_campaign(self, data):
        self.campaign_cursor += 1
        row = {
            "id": f"camp{self.campaign_cursor}",
            "name": data["name"],
            "subject": data["subject"],
            "from_email": data["from_email"],
            "from_name": data["from_name"],
            "html_content": data["html_content"],
            "plain_text_content": data.get("plain_text_content"),
            "batch_size": data.get("batch_size", 500),
            "send_rate_per_minute": data.get("send_rate_per_minute"),
            "stagger_seconds": data.get("stagger_seconds", 0),
            "schedule_at": data.get("schedule_at"),
            "window_start": data.get("window_start"),
            "window_end": data.get("window_end"),
            "window_timezone": data.get("window_timezone", "UTC"),
            "circuit_state": "ok",
            "status": data.get("status", "draft"),
            "priority": data.get("priority", "normal"),
            "stats": {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0},
            "created_at": "2026-01-01T00:00:00Z",
            "started_at": None,
            "finished_at": None,
            "reuse_root_id": data.get("reuse_root_id"),
        }
        self.campaigns.append(row)
        return row

    def get_campaign(self, campaign_id):
        return next((c for c in self.campaigns if c["id"] == campaign_id), None)

    def update_campaign(self, campaign_id, data):
        campaign = self.get_campaign(campaign_id)
        if campaign:
            campaign.update(data)
        return campaign

    def delete_campaign(self, campaign_id):
        before = len(self.campaigns)
        self.campaigns[:] = [c for c in self.campaigns if c["id"] != campaign_id]
        if len(self.campaigns) == before:
            return False
        self.recipients[:] = [r for r in self.recipients if r["campaign_id"] != campaign_id]
        return True

    def list_active_campaigns(self):
        return [c for c in self.campaigns if c["status"] in ("scheduled", "sending")]

    def list_sending_campaigns(self):
        return [c for c in self.campaigns if c["status"] == "sending"]

    def list_held_campaigns(self):
        return [c for c in self.campaigns if c["status"] == "held"]

    def list_engagement_campaigns(self, hours=24):
        from datetime import datetime, timedelta, timezone

        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        out = []
        for c in self.campaigns:
            if c["status"] == "sending":
                out.append(c)
            elif c["status"] == "completed" and str(c.get("finished_at") or "") >= since:
                out.append(c)
        return out

    def set_priority(self, campaign_id, priority):
        campaign = self.get_campaign(campaign_id)
        if campaign:
            campaign["priority"] = priority
        return campaign

    def add_recipients(self, campaign_id, contact_rows):
        rows = list(contact_rows)
        if not rows:
            return 0
        added = 0
        for contact_id in self.ensure_contacts(rows).values():
            if any(r["campaign_id"] == campaign_id and r["contact_id"] == contact_id for r in self.recipients):
                continue
            self.recipient_cursor += 1
            self.recipients.append({
                "id": f"r{self.recipient_cursor}",
                "campaign_id": campaign_id,
                "contact_id": contact_id,
                "status": "queued",
                "sent_at": None,
                "opened_at": None,
                "clicked_at": None,
                "claimed_at": None,
                "last_error": None,
            })
            added += 1
        return added

    def list_recipients(self, campaign_id, status=None, limit=1000):
        items = [r for r in self.recipients if r["campaign_id"] == campaign_id]
        if status:
            items = [r for r in items if r["status"] == status]
        return items[:limit]

    def update_recipient(self, recipient_id, data):
        self.bulk_update_recipients([recipient_id], data)

    def bulk_update_recipients(self, recipient_ids, data):
        for r in self.recipients:
            if r["id"] in recipient_ids:
                r.update(data)

    # ----- stored procedures -----
    def claim_recipients(self, campaign_id, limit):
        claimed = []
        for r in self.recipients:
            if r["campaign_id"] == campaign_id and r["status"] == "queued" and len(claimed) < limit:
                contact = next((c for c in self.contacts if c["id"] == r["contact_id"]), None)
                if contact is None or contact["status"] != "active" or not contact["safe_to_send"]:
                    continue
                r["status"] = "sending"
                r["claimed_at"] = "2026-01-01T00:00:00Z"
                claimed.append({
                    "recipient_id": r["id"],
                    "contact_id": contact["id"],
                    "email": contact["email"],
                    "first_name": contact["first_name"],
                    "last_name": contact["last_name"],
                    "metadata": contact.get("metadata") or {},
                })
        return claimed

    def requeue_stale_recipients(self, timeout_seconds=900):
        count = 0
        for r in self.recipients:
            if r["status"] == "sending" and r.get("claimed_at") is None:
                r["status"] = "queued"
                count += 1
        return count

    def count_recipients(self, campaign_id, status=None):
        return sum(
            1 for r in self.recipients
            if r["campaign_id"] == campaign_id and (status is None or r["status"] == status)
        )

    def count_sent_since(self, since_iso):
        return sum(
            1 for r in self.recipients
            if r.get("sent_at") and str(r["sent_at"]) >= since_iso
        )

    def list_due_scheduled(self):
        from app.core.scheduling import is_due

        return [
            c for c in self.campaigns
            if c["status"] == "scheduled" and is_due(c.get("schedule_at"))
        ]

    def suppress_contact(self, email, reason):
        for c in self.contacts:
            if c["email"] == email.lower().strip():
                c["status"] = reason
                c["safe_to_send"] = False

    # ----- events & stats -----
    def record_event(self, event_type, campaign_id=None, recipient_id=None,
                     url_clicked=None, ip_address=None, user_agent=None, raw_payload=None):
        self.events.append({
            "event_type": event_type,
            "campaign_id": campaign_id,
            "recipient_id": recipient_id,
            "url_clicked": url_clicked,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "raw_payload": raw_payload or {},
        })

    def get_campaign_metrics(self, campaign_id):
        campaign = self.get_campaign(campaign_id)
        return dict(campaign["stats"]) if campaign else {**self._default_stats()}

    def increment_stats(self, campaign_id, field, delta=1):
        campaign = self.get_campaign(campaign_id)
        if campaign:
            campaign["stats"][field] = campaign["stats"].get(field, 0) + delta

    def _default_stats(self):
        return {"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0}

    def get_settings_row(self):
        return self.settings_row

    def update_settings(self, data):
        if self.settings_row is None:
            self.settings_row = {
                "id": "s1",
                "sender_email": "no-reply@yourdomain.com",
                "sender_name": "Your Company",
                "sender_domain": "yourdomain.com",
                "max_hard_bounce_rate": 0.02,
                "max_complaint_rate": 0.001,
                "default_batch_size": 500,
                "send_rate_per_minute": 120,
                "max_recipients_per_hour": 2000,
                "max_recipients_per_day": 8000,
                "default_stagger_seconds": 0,
                "default_window_start": None,
                "default_window_end": None,
                "default_window_timezone": "UTC",
            }
        self.settings_row.update(data)
        return self.settings_row

    def ping(self):
        return True


@pytest.fixture()
def supabase():
    fake = FakeSupabase()
    original = supabase_module._singleton
    supabase_module._singleton = fake
    yield fake
    supabase_module._singleton = original


@pytest.fixture(autouse=True)
def _deterministic_sender(monkeypatch):
    """Isolate tests from the developer's real .env sender identity.

    The sender-domain lock is disabled in tests by using the config
    placeholder domain; dedicated tests exercise the lock explicitly.
    """
    monkeypatch.setenv("DEFAULT_SENDER_DOMAIN", "yourdomain.com")
    monkeypatch.setenv("DEFAULT_SENDER_EMAIL", "no-reply@yourdomain.com")
    # A public base URL so the send-time tracking/unsubscribe guard passes by
    # default; dedicated tests override it to exercise the guard itself.
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://mail.example.com")
    # Disable the auth wall by default; auth tests opt back in explicitly.
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "")
    monkeypatch.delenv("OPERATOR_EMAIL", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setattr("app.api.health._redis_alive", lambda: True)

    class TestingConfig:
        TESTING = True
        RATELIMIT_STORAGE_URI = "memory://"
        RATELIMIT_ENABLED = True
        BASE_URL = "http://localhost:5000"

    from app import create_app

    return create_app(TestingConfig)


@pytest.fixture()
def client(app):
    return app.test_client()
