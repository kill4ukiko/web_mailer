"""Seed the email-template gallery with the bundled starter templates.

Usage:
    python -m scripts.seed_templates

Idempotent by name: a template whose name already exists (case-insensitive) is
left untouched. Safe to run repeatedly and from setup-local.ps1.
"""
from __future__ import annotations

from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "app" / "seed_templates"

# filename -> (template name, default subject)
SEED_TEMPLATES = {
    "zoom_mail.html": ("Zoom Meeting Invitation", "Staff meeting invitation"),
    "docu_sight.html": ("DocuSign Document Request", "Please review and sign"),
}


def seed_templates(log=print) -> int:
    """Insert any missing seed templates; return the number inserted."""
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    existing = {t["name"].strip().lower() for t in supabase.list_templates()}

    inserted = 0
    for filename, (name, subject) in SEED_TEMPLATES.items():
        if name.strip().lower() in existing:
            log(f"skip (exists): {name}")
            continue
        path = SEED_DIR / filename
        if not path.exists():
            log(f"skip (missing file): {filename}")
            continue
        html = path.read_text(encoding="utf-8")
        supabase.create_template(name, subject, html)
        inserted += 1
        log(f"seeded: {name}")
    return inserted


def main() -> int:
    try:
        count = seed_templates()
    except Exception as exc:  # noqa: BLE001 - surface a clean message
        print(f"seeding failed: {exc}")
        return 1
    print(f"done — {count} template(s) inserted.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
