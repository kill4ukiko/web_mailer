"""Merge-tag substitution shared by email and SMS content.

Supported forms:

    {{first_name}}              contact field, empty when missing
    {{first_name|there}}        contact field with a fallback
    {{greeting_name}}           first name, else last name, else "there"
    {{full_name}}               "First Last", or whichever part exists
    {{unsubscribe}}             supplied by the caller (URL or STOP text)
    {{metadata.company}}        custom metadata value

Fallbacks are escaped with the rest of the value when HTML escaping is on.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Mapping

MERGE_RE = re.compile(
    r"\{\{\s*([A-Za-z0-9_.]+)\s*(?:\|\s*([^{}\n]*?))?\s*\}\}"
)


def _lookup(key: str, context: dict[str, Any], metadata: dict[str, Any]) -> str:
    # Support dotted access to metadata, e.g. {{metadata.company}}.
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


def _derived(key: str, context: dict[str, Any], metadata: dict[str, Any]) -> str | None:
    """Compute tags that are not stored directly on the contact."""
    if key == "greeting_name":
        explicit = _lookup(key, context, metadata).strip()
        if explicit:
            return explicit
        for field in ("first_name", "last_name"):
            value = _lookup(field, context, metadata).strip()
            if value:
                return value
        return "there"

    if key == "full_name":
        explicit = _lookup(key, context, metadata).strip()
        if explicit:
            return explicit
        parts = [
            _lookup("first_name", context, metadata).strip(),
            _lookup("last_name", context, metadata).strip(),
        ]
        return " ".join(part for part in parts if part)

    return None


def render_merge_tags(
    text: str,
    context: dict[str, Any] | None,
    *,
    specials: Mapping[str, str] | None = None,
    escape_html: bool = False,
) -> str:
    """Replace merge tags in ``text``.

    ``specials`` maps reserved tag names (for example ``unsubscribe``) to a
    caller-supplied value. Special values are returned verbatim, never escaped.
    """
    if not text:
        return text

    context = context or {}
    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}
    specials_lower = {key.lower(): value for key, value in (specials or {}).items()}

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        fallback = match.group(2)
        lowered = name.lower()

        if lowered in specials_lower:
            return specials_lower[lowered]

        value = _derived(lowered, context, metadata)
        if value is None:
            value = _lookup(name, context, metadata)

        if not value.strip() and fallback is not None:
            value = fallback

        return html_lib.escape(value) if escape_html else value

    return MERGE_RE.sub(_replace, text)
