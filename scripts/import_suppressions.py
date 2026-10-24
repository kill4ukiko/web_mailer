"""Import a suppression list (CSV or one email per line).

Usage:
    python -m scripts.import_suppressions addresses.csv
    python -m scripts.import_suppressions addresses.csv --apply

Dry-run is the default. ``--apply`` creates missing contacts with the chosen
suppression status and marks existing contacts as unsafe to send. This is the
only reliable way to backfill opt-outs that predate the durable event data;
the database alone cannot recover those addresses.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HEADER_VALUES = {"email", "e-mail", "address", "email address"}
_REASONS = ("unsubscribed", "bounced_hard", "bounced_soft", "complained", "invalid")


def load_emails(path: Path) -> tuple[list[str], list[str], int]:
    """Parse a file into normalized emails, invalid rows and duplicate count."""
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    emails: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    duplicates = 0
    email_index: int | None = None

    if rows:
        for index, value in enumerate(rows[0]):
            if str(value).strip().lower() in _HEADER_VALUES:
                email_index = index
                rows = rows[1:]
                break

    for row in rows:
        if email_index is not None:
            cell = (
                str(row[email_index]).strip()
                if email_index < len(row)
                else ""
            )
        else:
            cell = next((str(value).strip() for value in row if str(value).strip()), "")
        if not cell:
            continue
        email = cell.lower()
        if not _EMAIL_RE.match(email):
            invalid.append(cell)
            continue
        if email in seen:
            duplicates += 1
            continue
        seen.add(email)
        emails.append(email)

    return emails, invalid, duplicates


def import_suppressions(
    supabase,
    emails: list[str],
    reason: str,
    *,
    apply: bool = False,
) -> dict[str, int | bool | str]:
    """Resolve existing/new contacts and optionally apply suppression."""
    if reason not in _REASONS:
        raise ValueError(f"unsupported suppression reason: {reason}")

    unique = sorted(set(emails))
    existing = supabase.get_contacts_by_emails(unique)
    missing = [email for email in unique if email not in existing]

    applied = 0
    if apply and unique:
        if missing:
            supabase.ensure_contacts([{"email": email} for email in missing])
        applied = int(supabase.bulk_suppress_contacts(unique, reason) or 0)

    return {
        "total": len(unique),
        "existing": len(existing),
        "new": len(missing),
        "applied": applied,
        "dry_run": not apply,
        "reason": reason,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", type=Path, help="CSV or one email per line")
    parser.add_argument(
        "--reason",
        default="unsubscribed",
        choices=_REASONS,
        help="contact status to apply (default: unsubscribed)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write changes (default is a read-only dry run)",
    )
    args = parser.parse_args()

    try:
        emails, invalid, duplicates = load_emails(args.file)
    except OSError as exc:
        print(f"could not read {args.file}: {exc}")
        return 1

    if not emails:
        print("no valid email addresses found")
        return 1

    from app.core.supabase_client import get_supabase

    result = import_suppressions(
        get_supabase(), emails, args.reason, apply=args.apply
    )
    mode = "APPLIED" if args.apply else "DRY RUN (use --apply to write)"
    print(
        f"{mode}: total={result['total']} existing={result['existing']} "
        f"new={result['new']} reason={result['reason']} "
        f"duplicates={duplicates} invalid={len(invalid)}"
    )
    if args.apply:
        print(f"suppressed={result['applied']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
