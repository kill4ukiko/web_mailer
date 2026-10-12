"""Outbound email content processing.

Prepares each recipient's HTML/plain-text just-in-time before delivery:
  1. wrap bare HTML fragments in a minimal email shell
  2. guarantee an unsubscribe footer in HTML and plain-text
  3. merge-tag substitution ({{first_name}}, {{last_name}}, {{email}},
     {{unsubscribe}} and arbitrary/{{metadata.key}} values)
  4. click-tracking link rewriting (absolute http/https links only)
  5. open-tracking 1x1 pixel injection
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import quote

_HREF_RE = re.compile(r"""(href\s*=\s*)("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_HTML_SHELL_RE = re.compile(r"<!doctype\s+html|<html[\s>]", re.IGNORECASE)
_MERGE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
# Link targets we must never rewrite.
_SKIP_SCHEMES = ("mailto:", "tel:", "cid:", "data:", "javascript:")
_TRACK_PATH = "/api/track/"
_UNSUB_PATH = "/api/unsubscribe"

PIXEL_STYLE = "display:none;width:1px;height:1px;border:0;outline:none;"

_SHELL_HEAD = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "</head>"
)
_SHELL_BODY_STYLE = "margin:0;padding:0;background:#ffffff;"
_FOOTER_HTML = (
    '<p style="margin:24px 0 0;font-size:12px;line-height:1.5;color:#888888;">'
    "You are receiving this email because you subscribed. "
    'If you no longer wish to receive these emails, '
    '<a href="{{unsubscribe}}" style="color:#888888;text-decoration:underline;">'
    "unsubscribe here</a>.</p>"
)
_FOOTER_TEXT = "\n\n---\nUnsubscribe: {{unsubscribe}}"


def build_unsubscribe_url(base_url: str, campaign_id: str, recipient_id: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}{_UNSUB_PATH}?cid={quote(str(campaign_id))}&rid={quote(str(recipient_id))}"


def build_open_pixel_url(base_url: str, campaign_id: str, recipient_id: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}{_TRACK_PATH}{quote(str(campaign_id))}/{quote(str(recipient_id))}/open"


def build_click_url(
    base_url: str, campaign_id: str, recipient_id: str, target_url: str
) -> str:
    base = base_url.rstrip("/")
    encoded = quote(target_url, safe="")
    return f"{base}{_TRACK_PATH}{quote(str(campaign_id))}/{quote(str(recipient_id))}/click?target_url={encoded}"


def ensure_email_shell(html: str) -> str:
    """Wrap a bare HTML fragment in a minimal, email-safe document shell."""
    if not html:
        return html
    if _HTML_SHELL_RE.search(html):
        return html
    return (
        f"{_SHELL_HEAD}<body style=\"{_SHELL_BODY_STYLE}\">{html}</body></html>"
    )


def _has_unsubscribe(content: str) -> bool:
    lowered = content.lower()
    return (
        "{{unsubscribe" in lowered
        or "{{ unsubscribe" in lowered
        or _UNSUB_PATH in lowered
    )


def append_unsubscribe_footer(html: str, text: str | None) -> tuple[str, str | None]:
    """Ensure both the HTML and plain-text bodies carry an unsubscribe link."""
    if html and not _has_unsubscribe(html):
        if _BODY_CLOSE_RE.search(html):
            html = _BODY_CLOSE_RE.sub(lambda m: _FOOTER_HTML + m.group(0), html, count=1)
        else:
            html = html + _FOOTER_HTML
    if text is not None and not _has_unsubscribe(text):
        text = text + _FOOTER_TEXT
    return html, text


def substitute_merge_tags(text: str, context: dict[str, Any], unsubscribe_url: str) -> str:
    """Replace {{first_name}}/{{last_name}}/{{email}}/{{unsubscribe}} and custom tags."""
    if not text:
        return text

    metadata = context.get("metadata") if isinstance(context.get("metadata"), dict) else {}

    def _lookup(key: str) -> str:
        # Support dotted access to metadata, e.g. {{metadata.company}}.
        if key.lower().startswith("metadata."):
            sub = key.split(".", 1)[1]
            if sub in metadata:
                return str(metadata[sub])
            if sub in context:
                return str(context[sub])
            return ""
        for source in (context, metadata):
            if key in source:
                return "" if source[key] is None else str(source[key])
        return ""

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        lowered = name.lower()
        if lowered in ("unsubscribe", "unsubscribe_url"):
            return unsubscribe_url
        return _lookup(name)

    return _MERGE_RE.sub(_replace, text)


def rewrite_links(
    html: str, campaign_id: str, recipient_id: str, base_url: str
) -> str:
    """Wrap absolute http(s) links so clicks are tracked."""
    if not html:
        return html

    def _replace(match: re.Match) -> str:
        attr = match.group(1)
        value = match.group(3) if match.group(3) is not None else match.group(4)
        target = html_lib.unescape(value or "").strip()
        lowered = target.lower()

        if not lowered:
            return match.group(0)
        if lowered.startswith(_SKIP_SCHEMES):
            return match.group(0)
        # Only rewrite absolute http/https URLs to avoid breaking relative anchors.
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            return match.group(0)
        # Never re-wrap our own tracking/unsubscribe links (idempotency guard).
        if _TRACK_PATH in target or _UNSUB_PATH in target:
            return match.group(0)

        tracked = build_click_url(base_url, campaign_id, recipient_id, target)
        return f'{attr}"{tracked}"'

    return _HREF_RE.sub(_replace, html)


def inject_open_pixel(
    html: str, campaign_id: str, recipient_id: str, base_url: str
) -> str:
    """Insert a 1x1 transparent tracking pixel before </body> (or append)."""
    if not html:
        return html
    pixel = (
        f'<img src="{build_open_pixel_url(base_url, campaign_id, recipient_id)}" '
        f'width="1" height="1" alt="" style="{PIXEL_STYLE}" />'
    )
    if _BODY_CLOSE_RE.search(html):
        return _BODY_CLOSE_RE.sub(lambda m: pixel + m.group(0), html, count=1)
    return html + pixel


def render_email(
    html: str,
    plain_text: str | None,
    campaign_id: str,
    recipient_id: str,
    base_url: str,
    context: dict[str, Any] | None = None,
) -> tuple[str, str | None]:
    """Return (processed_html, processed_plain_text) for a single recipient."""
    context = context or {}
    unsubscribe_url = build_unsubscribe_url(base_url, campaign_id, recipient_id)

    processed_html = ensure_email_shell(html)
    processed_html, plain_text = append_unsubscribe_footer(processed_html, plain_text)

    processed_html = substitute_merge_tags(processed_html, context, unsubscribe_url)
    processed_html = rewrite_links(processed_html, campaign_id, recipient_id, base_url)
    processed_html = inject_open_pixel(processed_html, campaign_id, recipient_id, base_url)

    processed_text = plain_text
    if processed_text:
        processed_text = substitute_merge_tags(processed_text, context, unsubscribe_url)

    return processed_html, processed_text


# Sample merge-tag values used when rendering a preview (never sent).
PREVIEW_CONTEXT: dict[str, Any] = {
    "first_name": "Alex",
    "last_name": "Doe",
    "email": "alex@example.com",
}


def render_preview(
    html: str,
    plain_text: str | None = None,
    context: dict[str, Any] | None = None,
    base_url: str = "https://example.com",
) -> tuple[str, str | None]:
    """Render content exactly as a recipient sees it, minus live tracking.

    Adds the email shell + unsubscribe footer and substitutes merge tags with
    sample sample values. Unlike :func:`render_email` it does NOT rewrite links
    or inject the open pixel, since this is only for on-screen preview.
    """
    merged_context = {**PREVIEW_CONTEXT, **(context or {})}
    unsubscribe_url = build_unsubscribe_url(base_url, "preview", "preview")

    processed_html = ensure_email_shell(html or "")
    processed_html, plain_text = append_unsubscribe_footer(processed_html, plain_text)
    processed_html = substitute_merge_tags(processed_html, merged_context, unsubscribe_url)

    processed_text = plain_text
    if processed_text:
        processed_text = substitute_merge_tags(processed_text, merged_context, unsubscribe_url)

    return processed_html, processed_text
