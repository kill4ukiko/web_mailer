"""Webhook payload schemas (Brevo)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BrevoEvent(BaseModel):
    """Normalised representation of a Brevo event webhook payload."""

    event: Literal[
        "delivered", "opened", "click", "hard_bounce", "soft_bounce",
        "complaint", "unsubscribe", "failed",
    ]
    email: str = Field(alias="email")
    # Brevo's event webhooks embed this id automatically per recipient.
    # When available we use it to resolve campaign_recipients.
    recipient_id: str | None = None
    campaign_id: str | None = None
    click_url: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    ts: int | None = None

    model_config = {"populate_by_name": True}


class WebhookAck(BaseModel):
    received: bool = True
    processed: int = 0
    errors: list[str] = Field(default_factory=list)