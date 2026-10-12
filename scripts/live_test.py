"""Live end-to-end check against real Supabase.

By default Brevo delivery is stubbed so the database flow can be verified
without sending real email. Pass --real to actually deliver via Brevo.

Usage:
    python -m scripts.live_test                # DB flow only, no email sent
    python -m scripts.live_test --real         # real Brevo delivery
    python -m scripts.live_test --email you@example.com --real
"""
from __future__ import annotations

import argparse
import sys
import time

from app.config import get_settings
from app.core.brevo import SendResult
from app.core.supabase_client import get_supabase

DEFAULT_TEST_EMAIL = "deangary116@gmail.com"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default=DEFAULT_TEST_EMAIL)
    parser.add_argument("--real", action="store_true", help="actually send via Brevo")
    args = parser.parse_args()

    settings = get_settings()
    sb = get_supabase()

    print(f"[1] Upserting contact {args.email}")
    sb.create_contact({"email": args.email, "first_name": "Test", "last_name": "Recipient"})

    print("[2] Creating campaign")
    campaign = sb.create_campaign({
        "name": f"LIVE TEST {int(time.time())}",
        "subject": "Test from web_mailer",
        "from_email": settings.default_sender_email,
        "from_name": settings.default_sender_name,
        "html_content": (
            "<h1>Hello</h1><p>This is a live test from the web_mailer platform.</p>"
            "<p><a href='https://example.com'>Example link</a></p>"
            "<p><a href='{{unsubscribe}}'>Unsubscribe</a></p>"
        ),
        "plain_text_content": "Hello. This is a live test from web_mailer. Unsubscribe: {{unsubscribe}}",
        "batch_size": settings.default_batch_size,
        "batch_interval_ms": settings.default_batch_interval_ms,
        "throttle_rps": settings.default_throttle_rps,
    })
    campaign_id = campaign["id"]
    print(f"    campaign_id={campaign_id}")

    print("[3] Attaching recipient")
    added = sb.add_recipients(campaign_id, [{"email": args.email}])
    print(f"    recipients attached={added}")

    print("[4] Marking campaign as sending")
    sb.update_campaign(campaign_id, {"status": "sending", "started_at": "now()"})

    if not args.real:
        from app.core.brevo import BrevoClient

        def fake_send_batch(self, recipients, **kwargs):
            print(f"    [STUB] would send to {len(recipients)} recipient(s): "
                  f"{[r['email'] for r in recipients]}")
            return [
                SendResult(recipient_id=r["recipient_id"], email=r["email"], ok=True, message_id="stub")
                for r in recipients
            ]

        BrevoClient.send_batch = fake_send_batch
        print("[5] Running send_campaign task (STUBBED Brevo, no email sent)")
    else:
        print("[5] Running send_campaign task (REAL Brevo delivery)")

    from app.workers.tasks_campaign import send_campaign

    result = send_campaign.run(campaign_id)
    print(f"    task result: {result}")

    print("[6] Final campaign state")
    final = sb.get_campaign(campaign_id)
    print(f"    status={final['status']} circuit={final['circuit_state']} stats={final['stats']}")

    recipients = sb.list_recipients(campaign_id)
    for r in recipients:
        print(f"    recipient {r['id']} status={r['status']} sent_at={r['sent_at']}")

    if not args.real:
        print("\nNOTE: Brevo was stubbed. Re-run with --real once BREVO_API_KEY is a v3 API key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())