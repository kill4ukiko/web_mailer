"""Outbound SMS content processing.

Small, dependency-light helpers shared by the SMS composer, the send loop and
the report layer:

  * phone normalisation to E.164 (Brevo requires an international number)
  * sender-ID validation (11 alphanumeric / 15 numeric chars)
  * GSM-7 vs. UCS-2 detection and segment counting (credits == segments)
  * merge-tag substitution ({{first_name}}, {{last_name}}, {{phone}}, metadata.*)
  * optional STOP-code footer for marketing messages

Unlike :mod:`app.core.content` (email) there is no HTML, no tracking pixel and
no click rewriting: an SMS is a single plain-text body.
"""
from __future__ import annotations

import re
from typing import Any

from app.config import (
    SMS_SENDER_MAX_ALPHANUMERIC,
    SMS_SENDER_MAX_NUMERIC,
    SMS_SEGMENT_GSM,
    SMS_SEGMENT_GSM_MULTI,
    SMS_SEGMENT_UCS2,
    SMS_SEGMENT_UCS2_MULTI,
)

# GSM 03.38 basic character set. The escape character (0x1B) is not itself
# typeable; the extended set below is encoded with a leading escape and costs
# two septets each.
_GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_BASIC_SET = frozenset(_GSM7_BASIC)
# Characters that need the escape prefix (each counts as two septets).
_GSM7_EXTENDED_SET = frozenset("^{}\\[~]|€")

_PHONE_ALLOWED_RE = re.compile(r"^\+[1-9]\d{6,14}$")
_SENDER_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
_MERGE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

DEFAULT_STOP_HINT = "Reply STOP to opt out."


def is_gsm7(text: str) -> bool:
    """True when ``text`` fits the GSM-7 alphabet (no unicode fallback needed)."""
    for char in text or "":
        if char not in _GSM7_BASIC_SET and char not in _GSM7_EXTENDED_SET:
            return False
    return True


def gsm7_units(text: str) -> int:
    """Number of septets ``text`` occupies in GSM-7 (extended chars count 2)."""
    units = 0
    for char in text or "":
        units += 2 if char in _GSM7_EXTENDED_SET else 1
    return units


def utf16_units(text: str) -> int:
    """Number of UTF-16 code units (emoji/surrogate pairs count as 2)."""
    if not text:
        return 0
    return len(text.encode("utf-16-le")) // 2


def sms_segments(text: str, unicode_enabled: bool | None = None) -> int:
    """Number of SMS segments (== credits at one credit per segment).

    ``unicode_enabled`` forces the counting alphabet: ``True`` always uses
    UCS-2, ``False`` uses GSM-7 (and over-counts if the body is not GSM-7),
    ``None`` auto-detects.
    """
    if not text:
        return 0
    use_unicode = (not is_gsm7(text)) if unicode_enabled is None else bool(unicode_enabled)
    if use_unicode:
        units = utf16_units(text)
        if units <= SMS_SEGMENT_UCS2:
            return 1
        return 1 + (units - SMS_SEGMENT_UCS2 + SMS_SEGMENT_UCS2_MULTI - 1) // SMS_SEGMENT_UCS2_MULTI
    units = gsm7_units(text)
    if units <= SMS_SEGMENT_GSM:
        return 1
    return 1 + (units - SMS_SEGMENT_GSM + SMS_SEGMENT_GSM_MULTI - 1) // SMS_SEGMENT_GSM_MULTI


def normalise_phone(raw: str | None, default_country_code: str | None = None) -> str | None:
    """Best-effort conversion of ``raw`` to E.164, or None when invalid.

    Accepts ``+``-prefixed numbers, ``00`` international prefixes, and national
    numbers when a default country code is configured (a leading national ``0``
    is dropped). Formatting characters (spaces, dashes, parens, dots) are
    ignored. Returns None rather than guessing when the shape is wrong.
    """
    if not raw:
        return None
    value = re.sub(r"[\s().\-/]", "", str(raw).strip())
    if not value:
        return None
    if value.startswith("+"):
        candidate = value
    elif value.startswith("00"):
        candidate = "+" + value[2:]
    elif default_country_code:
        cc = re.sub(r"[^\d]", "", str(default_country_code))
        if not cc:
            return None
        national = value.lstrip("0")
        candidate = f"+{cc}{national}"
    else:
        return None
    if not _PHONE_ALLOWED_RE.match(candidate):
        return None
    return candidate


def is_valid_phone(raw: str | None, default_country_code: str | None = None) -> bool:
    return normalise_phone(raw, default_country_code) is not None


def sender_is_numeric(value: str | None) -> bool:
    return bool(value) and str(value).strip().isdigit()


def validate_sender(value: str | None, sender_type: str | None = None) -> str | None:
    """Return an error message when ``value`` is not a valid Brevo sender ID."""
    value = (value or "").strip()
    if not value:
        return "A sender ID is required."
    kind = (sender_type or ("numeric" if value.isdigit() else "alphanumeric")).lower()
    if kind == "numeric" or value.isdigit():
        digits = re.sub(r"\D", "", value)
        if len(digits) > SMS_SENDER_MAX_NUMERIC:
            return f"Sender number may have at most {SMS_SENDER_MAX_NUMERIC} digits."
        if len(digits) < 3:
            return "Sender number is too short."
        return None
    if not _SENDER_ID_RE.match(value):
        return "Sender ID may only contain letters and digits."
    if len(value) > SMS_SENDER_MAX_ALPHANUMERIC:
        return (
            f"Sender ID may have at most {SMS_SENDER_MAX_ALPHANUMERIC} "
            "alphanumeric characters."
        )
    return None


def stop_hint() -> str:
    return DEFAULT_STOP_HINT


def _has_stop(text: str) -> bool:
    lowered = (text or "").lower()
    return "stop" in lowered and ("reply" in lowered or "text" in lowered or "opt" in lowered)


def append_stop_footer(text: str) -> str:
    """Append the STOP instruction once (marketing messages only)."""
    if not text or _has_stop(text):
        return text
    return f"{text.rstrip()}\n\n{stop_hint()}"


def substitute_sms_tags(text: str, context: dict[str, Any] | None, stop_text: str = "STOP") -> str:
    """Replace {{first_name}}/{{last_name}}/{{phone}}/{{unsubscribe}} + metadata."""
    if not text:
        return text
    context = context or {}
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}

    def _lookup(key: str) -> str:
        if key.lower().startswith("metadata."):
            sub = key.split(".", 1)[1]
            if sub in metadata:
                return "" if metadata[sub] is None else str(metadata[sub])
            if sub in context:
                return "" if context[sub] is None else str(context[sub])
            return ""
        for source in (context, metadata):
            if key in source:
                return "" if source[key] is None else str(source[key])
        return ""

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        lowered = name.lower()
        if lowered in ("unsubscribe", "unsubscribe_url", "stop"):
            return stop_text
        return _lookup(name)

    return _MERGE_RE.sub(_replace, text)


def render_sms(
    content: str,
    context: dict[str, Any] | None = None,
    *,
    include_stop: bool = False,
    unicode_enabled: bool | None = None,
) -> tuple[str, int]:
    """Return the final (merge-tag substituted) body and its segment count.

    ``unicode_enabled`` may be forced on (Brevo's ``unicodeEnabled`` flag);
    otherwise the alphabet is auto-detected from the rendered body.
    """
    rendered = substitute_sms_tags(content or "", context)
    if include_stop:
        rendered = append_stop_footer(rendered)
    return rendered, sms_segments(rendered, unicode_enabled)


# Sample merge-tag values used when rendering a preview (never sent).
PREVIEW_CONTEXT: dict[str, Any] = {
    "first_name": "Alex",
    "last_name": "Doe",
    "phone": "+15551234567",
}
