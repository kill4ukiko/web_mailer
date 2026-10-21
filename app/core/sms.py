"""Brevo transactional-SMS delivery client.

The email channel talks to ``POST /smtp/email``; SMS talks to
``POST /transactionalSMS/send`` (legacy path ``/transactionalSMS/sms``). Each
message is billed in *credits* (roughly one per 160-char GSM segment), so the
outcome carries the segment/credit counters Brevo returns.

This client is transport only: pacing, claims, suppression and status bookkeeping
all live in the worker/cron layer (mirroring :mod:`app.core.brevo`).
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote

import requests

from app.config import Settings, get_settings
from app.core.brevo import BrevoError

logger = logging.getLogger(__name__)


@dataclass
class SmsOutcome:
    """Per-message result as reported by Brevo."""

    message_id: str | None = None
    segments: int | None = None
    credits_used: int | None = None
    remaining_credits: int | None = None


@dataclass
class SmsSendResult:
    recipient_id: str | None
    phone: str
    ok: bool
    message_id: str | None = None
    segments: int | None = None
    credits_used: int | None = None
    remaining_credits: int | None = None
    error: str | None = None


def build_webhook_url(base_url: str, token: str | None) -> str:
    """Per-message ``webUrl`` Brevo will POST delivery events to."""
    url = f"{base_url.rstrip('/')}/api/webhooks/sms"
    if token:
        url = f"{url}?token={quote(str(token))}"
    return url


class SmsClient:
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

    def send_sms(
        self,
        sender: str,
        recipient: str,
        content: str,
        sms_type: str = "transactional",
        tag: str | None = None,
        web_url: str | None = None,
        unicode_enabled: bool = False,
        organisation_prefix: str | None = None,
    ) -> SmsOutcome:
        """Send one SMS; returns the Brevo outcome (message id + credit counts).

        Falls back to the legacy ``/transactionalSMS/sms`` endpoint when the
        modern path is unavailable (older accounts), and tolerates both response
        shapes (``{messageId}`` vs ``{reference, messageId, smsCount, ...}``).
        """
        payload: dict[str, Any] = {
            "sender": sender,
            "recipient": recipient,
            "content": content,
            "type": sms_type or "transactional",
        }
        if tag:
            payload["tag"] = tag
        if web_url:
            payload["webUrl"] = web_url
        if organisation_prefix:
            payload["organisationPrefix"] = organisation_prefix
        if unicode_enabled:
            payload["unicodeEnabled"] = True

        resp = requests.post(
            f"{self.base_url}/transactionalSMS/send",
            json=payload,
            headers=self._headers,
            timeout=30,
        )
        if resp.status_code in (404, 405):
            resp = requests.post(
                f"{self.base_url}/transactionalSMS/sms",
                json=payload,
                headers=self._headers,
                timeout=30,
            )
        if resp.status_code not in (200, 201):
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise BrevoError(f"Brevo SMS send failed [{resp.status_code}]: {detail}")

        return self._parse_outcome(resp.json() or {})

    @staticmethod
    def _parse_outcome(data: dict[str, Any]) -> SmsOutcome:
        def _int(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        message_id = data.get("messageId") or data.get("reference")
        return SmsOutcome(
            message_id=str(message_id) if message_id is not None else None,
            segments=_int(data.get("smsCount")),
            credits_used=_int(data.get("usedCredits")),
            remaining_credits=_int(data.get("remainingCredits")),
        )

    def send_batch_sms(
        self,
        recipients: list[dict[str, Any]],
        sender: str,
        content: str,
        sms_type: str = "transactional",
        tag: str | None = None,
        unicode_enabled: bool = False,
        organisation_prefix: str | None = None,
        content_factory: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        web_url_factory: Callable[[dict[str, Any]], str | None] | None = None,
        send_rate_per_minute: int = 120,
        global_rate_per_minute: int | None = None,
        jitter_ms: tuple[int, int] = (0, 0),
        on_error: Callable[[Exception], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        gate: Any = None,
    ) -> list[SmsSendResult]:
        """Send a batch of SMS honouring the configured rate.

        Pacing mirrors :meth:`BrevoClient.send_batch`: an external ``gate``
        (shared, collision-free) when supplied, otherwise a local
        :class:`app.core.ratelimit.RateGate`. ``content_factory`` may override the
        body/unicode flag per recipient (for merge tags).

        recipients: dicts with keys ``recipient_id``, ``phone`` and optionally
                    ``first_name``/``last_name``/``metadata``.
        """
        from app.core.ratelimit import RateGate

        results: list[SmsSendResult] = []
        local_gate = None if gate is not None else RateGate(send_rate_per_minute, global_rate_per_minute)

        for index, recipient in enumerate(recipients):
            if cancel_check is not None and cancel_check():
                break

            if gate is not None:
                gate.wait()
            elif local_gate is not None:
                local_gate.wait()

            body = content
            unicode_flag = unicode_enabled
            if content_factory is not None:
                rendered = content_factory(recipient)
                body = rendered.get("content", content)
                unicode_flag = rendered.get("unicode", unicode_enabled)
            web_url = web_url_factory(recipient) if web_url_factory else None

            res = SmsSendResult(
                recipient_id=recipient.get("recipient_id"),
                phone=recipient.get("phone", ""),
                ok=False,
            )
            try:
                outcome = self.send_sms(
                    sender=sender,
                    recipient=recipient["phone"],
                    content=body,
                    sms_type=sms_type,
                    tag=tag,
                    web_url=web_url,
                    unicode_enabled=unicode_flag,
                    organisation_prefix=organisation_prefix,
                )
                res.ok = True
                res.message_id = outcome.message_id
                res.segments = outcome.segments
                res.credits_used = outcome.credits_used
                res.remaining_credits = outcome.remaining_credits
            except Exception as exc:  # noqa: BLE001 - surface per-recipient failures
                res.error = str(exc)
                if on_error:
                    on_error(exc)
            results.append(res)

            # Jitter only *between* sends; the gap after the last message is
            # carried by the caller's next-cycle countdown.
            if jitter_ms[1] > 0 and index < len(recipients) - 1:
                time.sleep(random.uniform(*jitter_ms) / 1000.0)
                if cancel_check is not None and cancel_check():
                    break

        return results

    def get_account_sms_credits(self) -> dict[str, Any] | None:
        """Return the account's SMS credit block, or None on failure.

        Brevo's ``GET /account`` returns ``plan`` as a list of entries, one per
        channel; the SMS entry is identified by ``type == "sms"``.
        """
        try:
            resp = requests.get(f"{self.base_url}/account", headers=self._headers, timeout=20)
        except requests.RequestException as exc:
            logger.warning("brevo account request failed: %s", exc)
            return None
        if not resp.ok:
            logger.warning("brevo account error [%s]: %s", resp.status_code, resp.text[:200])
            return None
        try:
            data = resp.json() or {}
        except ValueError:
            return None
        plans = data.get("plan")
        if isinstance(plans, dict):
            plans = [plans]
        for entry in plans or []:
            if isinstance(entry, dict) and str(entry.get("type", "")).lower() == "sms":
                return {
                    "type": "sms",
                    "credits": entry.get("credits"),
                    "credits_type": entry.get("creditsType"),
                }
        return None


_singleton: SmsClient | None = None


def get_sms_client() -> SmsClient:
    global _singleton
    if _singleton is None:
        _singleton = SmsClient()
    return _singleton
