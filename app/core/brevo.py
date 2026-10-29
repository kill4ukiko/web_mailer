"""Brevo (Sendinblue) HTTP API v3 delivery client."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from app.config import Settings, get_settings


class BrevoError(RuntimeError):
    """Raised on Brevo API failures."""


@dataclass
class SendResult:
    recipient_id: str
    email: str
    ok: bool
    message_id: str | None = None
    error: str | None = None


class BrevoClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.brevo_api_url.rstrip("/")
        self.api_key = self.settings.brevo_api_key

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def send_email(
        self,
        to_email: str,
        to_name: str,
        subject: str,
        html_content: str,
        plain_text: str | None,
        from_email: str,
        from_name: str,
        headers: dict[str, str] | None = None,
        sender_account: dict[str, Any] | None = None,
        reply_to: str | None = None,
        timeout: int = 30,
    ) -> str:
        """Send a single transactional email via POST /smtp/email.

        Returns the Brevo message_id on success.
        """
        payload: dict[str, Any] = {
            "sender": sender_account or {"email": from_email, "name": from_name},
            "to": [{"email": to_email, "name": to_name}],
            "subject": subject,
            "htmlContent": html_content,
        }
        if reply_to:
            payload["replyTo"] = {"email": reply_to}
        if plain_text:
            payload["textContent"] = plain_text
        if headers:
            payload["headers"] = headers

        resp = requests.post(
            f"{self.base_url}/smtp/email",
            json=payload,
            headers=self._headers,
            timeout=max(1, int(timeout)),
        )
        if resp.status_code not in (200, 201):
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise BrevoError(f"Brevo send failed [{resp.status_code}]: {detail}")

        return str((resp.json() or {}).get("messageId", ""))

    def send_batch(
        self,
        recipients: list[dict[str, Any]],
        subject: str,
        html_content: str,
        from_email: str,
        from_name: str,
        plain_text: str | None = None,
        headers_factory: Callable[[dict[str, Any]], dict[str, str]] | None = None,
        content_factory: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        send_rate_per_minute: int = 120,
        global_rate_per_minute: int | None = None,
        reply_to: str | None = None,
        jitter_ms: tuple[int, int] = (0, 0),
        on_error: Callable[[Exception], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        gate: Any = None,
    ) -> list[SendResult]:
        """Send a batch of emails honouring the configured send rate.

        Delivery is paced by ``gate`` when supplied — a
        :class:`app.core.pacing.PacingGate` that reserves globally unique,
        collision-free send slots across every campaign. Otherwise a local
        :class:`app.core.ratelimit.RateGate` paces this batch alone.

        ``send_rate_per_minute`` is the campaign's effective rate;
        ``global_rate_per_minute`` is the shared aggregate ceiling (used only
        when no external ``gate`` is provided).

        recipients: list of dicts with keys recipient_id, email, first_name,
                    last_name (plus any extras needed by headers_factory).

        content_factory: optional per-recipient renderer returning a dict with
                    optional keys ``subject``, ``html`` and ``text``. When
                    omitted the shared subject / html_content / plain_text are
                    used for every recipient.

        cancel_check: optional callable polled before each send; when it returns
                    True the batch stops early (used by the global kill switch).
        """
        from app.core.ratelimit import RateGate

        results: list[SendResult] = []
        local_gate = None if gate is not None else RateGate(send_rate_per_minute, global_rate_per_minute)

        for index, recipient in enumerate(recipients):
            if cancel_check is not None and cancel_check():
                break

            if gate is not None:
                gate.wait()
            elif local_gate is not None:
                local_gate.wait()

            headers = headers_factory(recipient) if headers_factory else {}
            content = content_factory(recipient) if content_factory else {}
            subject_body = content.get("subject", subject)
            html_body = content.get("html", html_content)
            text_body = content.get("text", plain_text)
            res = SendResult(
                recipient_id=recipient["recipient_id"],
                email=recipient["email"],
                ok=False,
            )
            try:
                message_id = self.send_email(
                    to_email=recipient["email"],
                    to_name=(
                        f"{recipient.get('first_name', '')} {recipient.get('last_name', '')}".strip()
                        or recipient["email"]
                    ),
                    subject=subject_body,
                    html_content=html_body,
                    plain_text=text_body,
                    from_email=from_email,
                    from_name=from_name,
                    headers=headers,
                    reply_to=reply_to,
                )
                res.ok = True
                res.message_id = message_id
            except Exception as exc:  # noqa: BLE001 - surface per-recipient failures
                res.error = str(exc)
                if on_error:
                    on_error(exc)
            results.append(res)

            # Jitter only *between* sends; the gap after the last email is
            # carried by the caller's next-cycle countdown.
            if jitter_ms[1] > 0 and index < len(recipients) - 1:
                time.sleep(random.uniform(*jitter_ms) / 1000.0)
                if cancel_check is not None and cancel_check():
                    break

        return results

    def verify_webhook(self, signature: str, payload: bytes, secret: str | None = None) -> bool:
        """Verify Brevo webhook HMAC-SHA256 signature."""
        import hashlib
        import hmac

        key = (secret or self.settings.brevo_webhook_secret or "").encode()
        if not key:
            return False
        digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, digest)
