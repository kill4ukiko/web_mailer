"""Brevo batch-send behaviour that does not need the network."""
from __future__ import annotations


class _NoopGate:
    def wait(self) -> None:
        return None


def test_send_batch_uses_per_recipient_subject(monkeypatch):
    from app.core.brevo import BrevoClient

    sent: list[dict] = []

    def fake_send_email(self, **kwargs):
        sent.append(kwargs)
        return f"mid{len(sent)}"

    monkeypatch.setattr("app.core.brevo.BrevoClient.send_email", fake_send_email)

    recipients = [
        {"recipient_id": "r1", "email": "ada@example.com", "first_name": "Ada", "last_name": "Lovelace"},
        {"recipient_id": "r2", "email": "anon@example.com", "first_name": None, "last_name": None},
    ]

    def content_factory(recipient):
        if recipient.get("first_name"):
            return {"subject": f"Hi {recipient['first_name']}"}
        return {}

    results = BrevoClient().send_batch(
        recipients=recipients,
        subject="Hello {{greeting_name}}",
        html_content="<p>Hello</p>",
        plain_text="Hello",
        from_email="no-reply@example.com",
        from_name="Sender",
        content_factory=content_factory,
        gate=_NoopGate(),
    )

    assert [r.ok for r in results] == [True, True]
    assert [message["subject"] for message in sent] == ["Hi Ada", "Hello {{greeting_name}}"]
