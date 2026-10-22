"""Bird webhook verification (Standard Webhooks / Svix-compatible HMAC)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any


class BirdWebhookError(ValueError):
    pass


def _secret_bytes(secret: str) -> bytes:
    value = (secret or "").strip()
    if value.startswith("whsec_"):
        value = value[len("whsec_"):]
    try:
        return base64.b64decode(value)
    except Exception as exc:  # noqa: BLE001
        raise BirdWebhookError("webhook secret is not valid base64") from exc


def verify_signature(raw_body: bytes, headers: Any, secret: str,
                     tolerance_seconds: int = 300) -> dict[str, Any]:
    """Verify a Standard Webhooks signature and return the decoded JSON body.

    Headers: ``webhook-id``, ``webhook-timestamp`` and one or more
    ``v1,<base64 signature>`` entries in ``webhook-signature``. The signed
    content is ``{id}.{timestamp}.{raw body}``.
    """
    if not secret:
        raise BirdWebhookError("Bird webhook secret is not configured")
    webhook_id = headers.get("webhook-id") or headers.get("Webhook-Id")
    timestamp = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp")
    signatures = headers.get("webhook-signature") or headers.get("Webhook-Signature")
    if not (webhook_id and timestamp and signatures):
        raise BirdWebhookError("missing webhook signature headers")
    try:
        ts = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise BirdWebhookError("invalid webhook timestamp") from exc
    if abs(time.time() - ts) > tolerance_seconds:
        raise BirdWebhookError("webhook timestamp outside tolerance")

    signed = f"{webhook_id}.{timestamp}.".encode() + raw_body
    expected = base64.b64encode(
        hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest()
    ).decode()
    provided = [part.strip() for part in str(signatures).split()]
    if not any(
        part.startswith("v1,") and hmac.compare_digest(expected, part[3:])
        for part in provided
    ):
        raise BirdWebhookError("invalid webhook signature")

    try:
        return json.loads(raw_body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise BirdWebhookError("webhook body is not valid JSON") from exc
