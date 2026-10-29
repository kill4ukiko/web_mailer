"""Seed the email-template gallery with the bundled starter templates.

Usage:
    python -m scripts.seed_templates
    python -m scripts.seed_templates --dry-run
    python -m scripts.seed_templates --only google_meet_client_meeting_invitation
    python -m scripts.seed_templates --update-existing

Each template is a pair of files in ``app/seed_templates``: ``<stem>.html`` and
an optional ``<stem>.txt`` plain-text version. Idempotent by name: a template
whose name already exists (case-insensitive) is left untouched unless
``--update-existing`` is passed. Safe to run repeatedly and from
setup-local.ps1.
"""
from __future__ import annotations

import argparse
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "app" / "seed_templates"

# (file stem, template name, default subject)
SEED_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    # Original bundled starters (HTML only).
    ("zoom_mail", "Zoom Meeting Invitation", "Staff meeting invitation"),
    ("docu_sight", "DocuSign Document Request", "Please review and sign"),
    # Meetings
    (
        "google_meet_client_meeting_invitation",
        "Google Meet: Client Meeting Invitation",
        "You're invited: Google Meet with [Company Name]",
    ),
    (
        "zoom_client_meeting_confirmation",
        "Zoom: Client Meeting Confirmation",
        "Confirmed: Zoom call on [Date]",
    ),
    (
        "microsoft_teams_project_meeting_invitation",
        "Microsoft Teams: Project Meeting Invitation",
        "Teams meeting invitation - [Project Name]",
    ),
    ("webex_meeting_invitation", "Webex: Meeting Invitation", "Webex meeting invitation: [Topic]"),
    (
        "in_person_meeting_invitation",
        "In-Person Meeting Invitation",
        "Invitation: [Topic] at [Location]",
    ),
    (
        "phone_appointment_confirmation",
        "Phone Appointment Confirmation",
        "Your call with [Company Name] is confirmed",
    ),
    (
        "meeting_agenda_and_pre_read",
        "Meeting Agenda & Pre-Read",
        "Agenda and pre-read for [Topic]",
    ),
    (
        "meeting_reminder_tomorrow",
        "Meeting Reminder: Tomorrow",
        "Reminder: [Topic] tomorrow at [Meeting Time]",
    ),
    ("meeting_rescheduled", "Meeting Rescheduled", "New time: [Topic] moved to [New Date]"),
    ("meeting_cancelled", "Meeting Cancelled", "Cancelled: [Topic] on [Date]"),
    (
        "post_meeting_follow_up",
        "Post-Meeting Follow-Up",
        "Thanks for meeting - next steps and recording",
    ),
    (
        "video_interview_invitation",
        "Video Interview Invitation",
        "Interview invitation: [Role] at [Company Name]",
    ),
    # Webinars, sales and partnerships
    (
        "webinar_and_workshop_invitation",
        "Webinar & Workshop Invitation",
        "Join us: [Webinar Title] on [Date]",
    ),
    (
        "sales_introduction_and_discovery_call",
        "Sales Introduction & Discovery Call",
        "Quick question about [Company Name]'s [Goal]",
    ),
    (
        "product_demo_invitation",
        "Product Demo Invitation",
        "See [Product] in action - 20-minute demo",
    ),
    (
        "proposal_and_quotation_delivery",
        "Proposal & Quotation Delivery",
        "Proposal for [Project Name] - ready for review",
    ),
    ("partnership_outreach", "Partnership Outreach", "Partnership idea for [Company Name]"),
    (
        "event_and_conference_invitation",
        "Event & Conference Invitation",
        "You're invited: [Event Name] on [Date]",
    ),
    # Delivery, projects and billing
    ("project_kickoff", "Project Kickoff", "Kickoff confirmed: [Project Name]"),
    (
        "project_status_update",
        "Project Status Update",
        "[Project Name] - weekly status update",
    ),
    (
        "invoice_payment_reminder",
        "Invoice Payment Reminder",
        "Friendly reminder: invoice [Invoice Number] due [Due Date]",
    ),
    ("payment_receipt", "Payment Receipt", "Receipt for invoice [Invoice Number]"),
    (
        "contract_renewal_reminder",
        "Contract Renewal Reminder",
        "Your [Product/Service] renewal is coming up",
    ),
    (
        "scheduled_maintenance_notice",
        "Scheduled Maintenance Notice",
        "Planned maintenance on [Date] from [Start Time] to [End Time]",
    ),
    # Customer lifecycle
    (
        "customer_onboarding_welcome",
        "Customer Onboarding Welcome",
        "Welcome aboard, {{first_name}} - your next steps",
    ),
    (
        "support_case_resolved_and_csat",
        "Support Case Resolved & CSAT",
        "Your support case #[Case Number] is resolved",
    ),
    (
        "service_disruption_apology",
        "Service Disruption Apology",
        "An update on the [Service] disruption",
    ),
    (
        "quarterly_business_review",
        "Quarterly Business Review",
        "Your Q[Quarter] review with [Company Name]",
    ),
    (
        "feedback_survey_invitation",
        "Feedback Survey Invitation",
        "We'd value your feedback on [Topic]",
    ),
    (
        "customer_check_in",
        "Customer Check-In",
        "Checking in - how is [Product/Service] working for you?",
    ),
    # People and operations
    (
        "new_hire_welcome",
        "New Hire Welcome",
        "Welcome to [Company Name], {{first_name}}",
    ),
    (
        "company_announcement",
        "Company Announcement",
        "[Company Name] update: [Announcement Title]",
    ),
)


def read_template(stem: str) -> tuple[str | None, str | None]:
    """Return ``(html, plain_text)`` for a template stem, or ``(None, None)``."""
    html_path = SEED_DIR / f"{stem}.html"
    text_path = SEED_DIR / f"{stem}.txt"
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else None
    text = text_path.read_text(encoding="utf-8") if text_path.exists() else None
    return html, text


def _matches(only: set[str] | None, stem: str, name: str) -> bool:
    if not only:
        return True
    wanted = {value.strip().lower() for value in only if value.strip()}
    return stem.lower() in wanted or name.strip().lower() in wanted


def seed_templates(
    log=print,
    *,
    dry_run: bool = False,
    only: set[str] | None = None,
    update_existing: bool = False,
) -> int:
    """Insert any missing seed templates; return the number inserted.

    With ``update_existing`` an existing row is refreshed (subject, HTML and
    plain text) instead of skipped. Names are never changed.
    """
    from app.core.supabase_client import get_supabase

    supabase = get_supabase()
    existing = {
        t["name"].strip().lower(): t
        for t in supabase.list_templates()
    }

    inserted = 0
    for stem, name, subject in SEED_TEMPLATES:
        if not _matches(only, stem, name):
            continue

        html, text = read_template(stem)
        if html is None:
            log(f"skip (missing file): {stem}.html")
            continue

        entry = existing.get(name.strip().lower())
        if entry is not None and not update_existing:
            log(f"skip (exists): {name}")
            continue

        if dry_run:
            action = "update" if entry is not None else "insert"
            log(f"dry-run: would {action}: {name}")
            inserted += 1
            continue

        if entry is not None:
            supabase.update_template(
                entry["id"],
                {
                    "subject": subject,
                    "html_content": html,
                    "plain_text_content": text,
                },
            )
            log(f"updated: {name}")
            continue

        supabase.create_template(name, subject, html, plain_text_content=text)
        inserted += 1
        log(f"seeded: {name}")
    return inserted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed bundled email templates.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be inserted or updated without writing.",
    )
    parser.add_argument(
        "--update-existing",
        action="store_true",
        help="Refresh templates whose names already exist (default: skip).",
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="STEM_OR_NAME",
        help="Limit to one template stem or name (repeatable).",
    )
    args = parser.parse_args(argv)

    try:
        count = seed_templates(
            dry_run=args.dry_run,
            only=set(args.only) if args.only else None,
            update_existing=args.update_existing,
        )
    except Exception as exc:  # noqa: BLE001 - surface a clean message
        print(f"seeding failed: {exc}")
        return 1

    if args.dry_run:
        print(f"dry run — {count} template(s) would be written.")
    elif args.update_existing:
        print("done — existing templates refreshed (see log above).")
    else:
        print(f"done — {count} template(s) inserted.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
