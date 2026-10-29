"""Checks for the bundled white-label email-template catalog."""
from __future__ import annotations

import re

import pytest

from scripts import seed_templates as seeder

MERGE_TAG_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
ALLOWED_TAGS = {
    "first_name",
    "last_name",
    "full_name",
    "greeting_name",
    "email",
    "unsubscribe",
    "unsubscribe_url",
}
# Names already present in the shared database; new seeds must not collide.
RESERVED_NAMES = {"esecure file delivery", "google meeting invitation"}
LEGACY_STEMS = {"zoom_mail", "docu_sight"}

TEMPLATE_CASES = [pytest.param(entry, id=entry[0]) for entry in seeder.SEED_TEMPLATES]


@pytest.mark.parametrize("entry", TEMPLATE_CASES)
def test_seed_template_html_is_well_formed(entry):
    stem, name, subject = entry
    html, _ = seeder.read_template(stem)

    assert name.strip() and subject.strip()
    assert html, f"{stem}.html is missing"

    if stem in LEGACY_STEMS:
        return

    assert "<!-- Preheader -->" in html
    assert "max-width:600px" in html
    assert 'role="presentation"' in html
    assert "{{greeting_name}}" in html
    assert "{{unsubscribe}}" in html
    assert 'href="https://' in html
    assert html.count('href="https://') >= 2
    assert "javascript:" not in html.lower()
    assert "data:text/html" not in html.lower()


@pytest.mark.parametrize("entry", TEMPLATE_CASES)
def test_seed_template_plain_text_matches(entry):
    stem, _, _ = entry
    _, text = seeder.read_template(stem)

    if stem in LEGACY_STEMS:
        assert text is None
        return

    assert text and text.strip()
    assert "Hello {{greeting_name}}," in text
    assert "{{unsubscribe}}" in text
    assert "https://" in text
    assert not re.search(r"<(?:a|div|p|table|td|tr|h1|ul|li)\b", text, re.IGNORECASE)


@pytest.mark.parametrize("entry", TEMPLATE_CASES)
def test_seed_template_uses_supported_merge_tags_only(entry):
    stem, _, _ = entry
    html, text = seeder.read_template(stem)

    tags = {tag.split(".", 1)[0] for tag in MERGE_TAG_RE.findall(html or "")}
    tags |= {tag.split(".", 1)[0] for tag in MERGE_TAG_RE.findall(text or "")}
    assert tags <= ALLOWED_TAGS, f"{stem} uses unsupported tags: {tags - ALLOWED_TAGS}"


def test_seed_names_are_unique_and_do_not_collide():
    names = [name.strip().lower() for _, name, _ in seeder.SEED_TEMPLATES]
    assert len(names) == len(set(names))
    assert len(names) >= 34

    for stem, name, _ in seeder.SEED_TEMPLATES:
        if stem in LEGACY_STEMS:
            continue
        assert name.strip().lower() not in RESERVED_NAMES


def test_new_catalog_has_32_templates():
    new = [entry for entry in seeder.SEED_TEMPLATES if entry[0] not in LEGACY_STEMS]
    assert len(new) == 32


class FakeSupabase:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.created = []
        self.updated = []

    def list_templates(self):
        return list(self.rows)

    def create_template(self, name, subject, html_content, plain_text_content=None):
        row = {
            "id": f"tpl{len(self.rows) + 1}",
            "name": name,
            "subject": subject,
            "html_content": html_content,
            "plain_text_content": plain_text_content,
        }
        self.rows.append(row)
        self.created.append(row)
        return row

    def update_template(self, template_id, data):
        for row in self.rows:
            if row["id"] == template_id:
                row.update(data)
                self.updated.append((template_id, data))
                return row
        return None


def _patch_supabase(monkeypatch, fake):
    from app.core import supabase_client

    monkeypatch.setattr(supabase_client, "get_supabase", lambda: fake)


def test_seeder_inserts_missing_templates_with_plain_text(monkeypatch):
    fake = FakeSupabase()
    _patch_supabase(monkeypatch, fake)

    inserted = seeder.seed_templates(log=lambda _msg: None)

    assert inserted == len(seeder.SEED_TEMPLATES)
    assert len(fake.created) == len(seeder.SEED_TEMPLATES)
    by_name = {row["name"]: row for row in fake.created}
    meet = by_name["Google Meet: Client Meeting Invitation"]
    assert meet["plain_text_content"]
    assert "Join the Google Meet:" in meet["plain_text_content"]
    for stem, name, _ in seeder.SEED_TEMPLATES:
        if stem not in LEGACY_STEMS:
            assert by_name[name]["plain_text_content"]


def test_seeder_is_idempotent(monkeypatch):
    fake = FakeSupabase()
    _patch_supabase(monkeypatch, fake)

    first = seeder.seed_templates(log=lambda _msg: None)
    second = seeder.seed_templates(log=lambda _msg: None)

    assert first == len(seeder.SEED_TEMPLATES)
    assert second == 0
    assert len(fake.created) == first


def test_seeder_dry_run_writes_nothing(monkeypatch):
    fake = FakeSupabase()
    _patch_supabase(monkeypatch, fake)

    would_write = seeder.seed_templates(log=lambda _msg: None, dry_run=True)

    assert would_write == len(seeder.SEED_TEMPLATES)
    assert fake.created == []
    assert fake.updated == []


def test_seeder_skips_existing_by_default(monkeypatch):
    existing = {
        "id": "tpl-existing",
        "name": "Google Meet: Client Meeting Invitation",
        "subject": "Old subject",
        "html_content": "<p>old</p>",
        "plain_text_content": None,
    }
    fake = FakeSupabase([existing])
    _patch_supabase(monkeypatch, fake)

    inserted = seeder.seed_templates(log=lambda _msg: None)

    assert inserted == len(seeder.SEED_TEMPLATES) - 1
    assert existing["subject"] == "Old subject"
    assert fake.updated == []


def test_seeder_can_refresh_existing_templates(monkeypatch):
    existing = {
        "id": "tpl-existing",
        "name": "Google Meet: Client Meeting Invitation",
        "subject": "Old subject",
        "html_content": "<p>old</p>",
        "plain_text_content": None,
    }
    fake = FakeSupabase([existing])
    _patch_supabase(monkeypatch, fake)

    seeder.seed_templates(log=lambda _msg: None, update_existing=True)

    assert fake.updated
    template_id, data = fake.updated[0]
    assert template_id == "tpl-existing"
    assert data["subject"] == "You're invited: Google Meet with [Company Name]"
    assert "Join the Google Meet" in data["html_content"]
    assert data["plain_text_content"]


def test_seeder_only_filter(monkeypatch):
    fake = FakeSupabase()
    _patch_supabase(monkeypatch, fake)

    inserted = seeder.seed_templates(
        log=lambda _msg: None,
        only={"google_meet_client_meeting_invitation"},
    )

    assert inserted == 1
    assert [row["name"] for row in fake.created] == ["Google Meet: Client Meeting Invitation"]
