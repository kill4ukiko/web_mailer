"""Apply the sql/*.sql migrations to the database in DATABASE_URL.

Usage:
    python -m scripts.apply_sql                 # apply pending migrations
    python -m scripts.apply_sql --only 11_campaign_reuse.sql
    python -m scripts.apply_sql --force         # re-run everything

Applied filenames are recorded in ``schema_migrations`` so a redeploy only runs
new files. Migration files are expected to be idempotent where possible; older
files (e.g. 01_schema.sql) create types/tables unconditionally, so re-running
is tolerated: each file is applied independently and a failure is reported
without aborting the remaining files.
"""
from __future__ import annotations

from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
ORDER = [
    "01_schema.sql",
    "02_indexes.sql",
    "03_functions.sql",
    "04_campaign_scheduling.sql",
    "05_mailer_defaults.sql",
    "06_sending_safety.sql",
    "07_contact_lists.sql",
    "08_brevo_plan_and_guard.sql",
    "09_send_engine.sql",
    "10_remove_qev.sql",
    "11_campaign_reuse.sql",
    "12_email_templates.sql",
    "13_campaign_hold.sql",
    "14_reputation_lifecycle.sql",
    "15_paced_next_send.sql",
    "16_sms.sql",
    "17_telegram_sections.sql",
    "18_bird_sms.sql",
    "19_atomic_stats.sql",
]

_TRACKING_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _applied_migrations(cur) -> set[str]:
    cur.execute("SELECT filename FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def apply_migrations(
    files: list[str] | None = None, log=print, force: bool = False
) -> list[str]:
    """Apply pending migrations; return failed filenames.

    Each file runs independently on one connection so a single "already
    exists" error does not abort the remaining files. Files already recorded in
    ``schema_migrations`` are skipped unless ``force`` is set.
    """
    import psycopg2

    from app.config import get_settings

    filenames = files or ORDER
    database_url = get_settings().database_url
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")

    failures: list[str] = []
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(_TRACKING_TABLE_SQL)
            conn.commit()
            applied = _applied_migrations(cur)
            for filename in filenames:
                path = SQL_DIR / filename
                if not path.exists():
                    log(f"skip (missing): {filename}")
                    continue
                if filename in applied and not force:
                    log(f"skip (already applied): {filename}")
                    continue
                log(f"applying {filename} ...")
                try:
                    cur.execute(path.read_text(encoding="utf-8-sig"))
                    cur.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s) "
                        "ON CONFLICT (filename) DO UPDATE SET applied_at = NOW()",
                        (filename,),
                    )
                    conn.commit()
                except Exception as exc:  # noqa: BLE001 - report and continue
                    conn.rollback()
                    failures.append(filename)
                    log(f"  warning: {exc}")
    finally:
        conn.close()
    return failures


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None,
                        help="apply only these migration file(s)")
    parser.add_argument("--force", action="store_true",
                        help="re-run files even if schema_migrations lists them")
    args = parser.parse_args()
    try:
        failures = apply_migrations(args.only, force=args.force)
    except Exception as exc:  # noqa: BLE001 - connection/config errors
        print(f"migration failed: {exc}")
        return 1
    if failures:
        print(f"completed with warnings for: {', '.join(failures)}")
        return 1
    print("migrations applied successfully.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
