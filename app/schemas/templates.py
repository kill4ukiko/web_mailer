"""Reusable email-template request schemas."""
from __future__ import annotations

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    subject: str = Field(default="", max_length=255)
    html_content: str = Field(min_length=1)
    plain_text_content: str | None = None


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    subject: str | None = Field(default=None, max_length=255)
    html_content: str | None = Field(default=None, min_length=1)
    plain_text_content: str | None = None
