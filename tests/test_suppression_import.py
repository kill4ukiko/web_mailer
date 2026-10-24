"""Suppression-list import: parsing, dry-run, and idempotent application."""
from __future__ import annotations

from scripts.import_suppressions import import_suppressions, load_emails


def test_load_emails_normalizes_and_reports_bad_rows(tmp_path):
    path = tmp_path / "suppressions.csv"
    path.write_text(
        "Email\nA@Example.com\na@example.com\nnot-an-email\n\nb@example.com\n",
        encoding="utf-8",
    )

    emails, invalid, duplicates = load_emails(path)

    assert emails == ["a@example.com", "b@example.com"]
    assert invalid == ["not-an-email"]
    assert duplicates == 1


def test_load_emails_uses_the_named_email_column(tmp_path):
    path = tmp_path / "brevo.csv"
    path.write_text(
        "ID,Email,Status\n1,first@example.com,blocked\n2,second@example.com,unsubscribed\n",
        encoding="utf-8",
    )

    emails, invalid, duplicates = load_emails(path)

    assert emails == ["first@example.com", "second@example.com"]
    assert invalid == []
    assert duplicates == 0


def test_import_is_dry_run_by_default(supabase):
    existing = supabase.create_contact({"email": "known@example.com"})[0]

    result = import_suppressions(
        supabase,
        ["known@example.com", "new@example.com"],
        "unsubscribed",
        apply=False,
    )

    assert result["dry_run"] is True
    assert result["existing"] == 1
    assert result["new"] == 1
    assert result["applied"] == 0
    assert existing["status"] == "active"
    assert len(supabase.contacts) == 1


def test_import_apply_suppresses_existing_and_creates_unknown(supabase):
    existing = supabase.create_contact({"email": "known@example.com"})[0]

    result = import_suppressions(
        supabase,
        ["known@example.com", "new@example.com", "known@example.com"],
        "unsubscribed",
        apply=True,
    )

    assert result["total"] == 2
    assert result["existing"] == 1
    assert result["new"] == 1
    assert result["applied"] == 2

    assert existing["status"] == "unsubscribed"
    assert existing["safe_to_send"] is False
    created = supabase._contact_by_email("new@example.com")
    assert created is not None
    assert created["status"] == "unsubscribed"
    assert created["safe_to_send"] is False

    # Re-running is idempotent and simply discovers both rows as existing.
    result = import_suppressions(
        supabase,
        ["known@example.com", "new@example.com"],
        "unsubscribed",
        apply=True,
    )
    assert result["existing"] == 2
    assert result["new"] == 0
    assert result["applied"] == 2
