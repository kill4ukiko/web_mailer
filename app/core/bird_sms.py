"""Bird SMS API client (current Bird platform, bk_ workspace keys).

Transport only: pacing, claims, suppression and status bookkeeping live in the
worker layer. Bird sends one recipient per request and returns 202 Accepted;
final delivery arrives on signed webhooks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from app.config import BIRD_API_HOSTS, BIRD_DEFAULT_REGION

logger = logging.getLogger(__name__)


class BirdSmsError(RuntimeError):
    def __init__(self, message: str, status: int | None = None,
                 code: str | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload


@dataclass
class BirdOutcome:
    message_id: str | None = None
    status: str | None = None
    accepted: bool = True


def build_webhook_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/api/webhooks/bird"


def _host(region: str | None) -> str:
    region = (region or BIRD_DEFAULT_REGION).lower()
    return BIRD_API_HOSTS.get(region, BIRD_API_HOSTS[BIRD_DEFAULT_REGION]).rstrip("/")


class BirdSmsClient:
    def __init__(self, api_key: str | None = None, region: str | None = None) -> None:
        if api_key is None or region is None:
            from app.core.bird_sms_config import effective_bird_settings
            from app.core.bird_sms_store import get_bird_store

            settings = effective_bird_settings(None)
            api_key = api_key or settings.get("bird_api_key") or ""
            region = region or settings.get("bird_region")
        self.api_key = (api_key or "").strip()
        self.region = region or BIRD_DEFAULT_REGION
        self.base_url = _host(self.region)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise BirdSmsError("Bird API key is not configured", code="missing_api_key")
        resp = requests.post(
            f"{self.base_url}{path}", json=payload, headers=self.headers, timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code not in (200, 201, 202):
            error = data.get("error") if isinstance(data, dict) else None
            error = error if isinstance(error, dict) else {}
            code = error.get("code") or data.get("code") if isinstance(data, dict) else None
            detail = (
                error.get("description")
                or error.get("message")
                or (data.get("message") if isinstance(data, dict) else None)
                or resp.text[:500]
            )
            raise BirdSmsError(
                f"Bird SMS send failed [{resp.status_code}]: {detail}",
                status=resp.status_code, code=code, payload=data,
            )
        return data if isinstance(data, dict) else {}

    def send_message(self, to: str, *, from_number: str | None = None,
                     text: str | None = None, category: str | None = None,
                     smart_encoding: bool | None = None,
                     template: dict[str, Any] | None = None) -> BirdOutcome:
        payload: dict[str, Any] = {"to": to}
        if template:
            tpl = {k: v for k, v in template.items() if v is not None}
            payload["template"] = tpl
            # Built-in/system templates must not include a from number; Bird
            # rejects reserved fields. Workspace templates do require one.
            if from_number and str(tpl.get("scope") or "").lower() == "workspace":
                payload["from"] = from_number
        else:
            if not text:
                raise BirdSmsError("text is required for a free-text message", code="missing_text")
            payload["text"] = text
            if from_number:
                payload["from"] = from_number
            if category:
                payload["category"] = category
        if smart_encoding is not None:
            payload["smart_encoding"] = bool(smart_encoding)
        data = self._post("/v1/sms/messages", payload)
        message_id = data.get("id") or data.get("message_id") or data.get("messageId")
        return BirdOutcome(message_id=str(message_id) if message_id else None,
                           status=str(data.get("status") or "accepted"))

    def list_templates(self, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch the workspace template catalogue (needs sms_management scope)."""
        key = self.api_key
        from app.core.bird_sms_config import effective_bird_settings
        settings = effective_bird_settings(None)
        key = settings.get("bird_management_api_key") or key
        if not key:
            raise BirdSmsError("Bird management key is not configured", code="missing_management_key")
        resp = requests.get(
            f"{self.base_url}/v1/sms/templates",
            params={"limit": limit},
            headers={**self.headers, "Authorization": f"Bearer {key}"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise BirdSmsError(
                f"Bird template catalogue failed [{resp.status_code}]: {resp.text[:300]}",
                status=resp.status_code,
            )
        data = resp.json() or {}
        return data.get("items") or data.get("data") or []

    def get_balance(self) -> dict[str, Any]:
        """Wallet balances are only available to org OAuth tokens; not supported."""
        raise BirdSmsError(
            "Bird wallet balance requires an organization OAuth token with "
            "org:billing:read; use the locally tracked spend instead",
            code="wallet_not_supported",
        )


def parse_error_payload(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Extract ``(code, description, carrier_code)`` from a Bird delivery event."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, None, None
    return (
        error.get("code"),
        error.get("description") or error.get("message"),
        error.get("carrier_error_code"),
    )
