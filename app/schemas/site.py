"""Validation schemas for public website accounts."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, model_validator


class SiteRegistration(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=10, max_length=128)
    confirm_password: str = Field(min_length=1, max_length=128)
    marketing_consent: bool = False
    terms_accepted: bool = False

    @model_validator(mode="after")
    def _passwords_match(self):
        if self.password != self.confirm_password:
            raise ValueError("passwords do not match")
        return self

    @model_validator(mode="after")
    def _terms_accepted(self):
        if not self.terms_accepted:
            raise ValueError("you must accept the terms and privacy policy")
        return self


class SiteLogin(BaseModel):
    email: EmailStr = Field(max_length=255)
    password: str = Field(min_length=1, max_length=128)
