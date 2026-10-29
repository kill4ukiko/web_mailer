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

import hashlib
import hmac
import html as html_lib
import re
from typing import Any
from urllib.parse import quote

from flask import current_app, has_app_context

from app.core.merge_tags import render_merge_tags

_HREF_RE = re.compile(r"""(href\s*=\s*)("([^"]*)"|'([^']*)')""", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_HTML_SHELL_RE = re.compile(r"<!doctype\s+html|<html[\s>]", re.IGNORECASE)
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


def _settings_value(name: str) -> str:
    from app.config import get_settings

    if has_app_context():
        settings_obj = current_app.config.get("SETTINGS")
        value = getattr(settings_obj, name, None)
        if value:
            return str(value)
    return str(getattr(get_settings(), name, "") or "")


def _unsubscribe_secret() -> str:
    """Primary secret used to sign new unsubscribe links."""
    dedicated = _settings_value("unsubscribe_token_secret")
    if dedicated:
        return dedicated
    if has_app_context():
        value = current_app.config.get("SECRET_KEY")
        if value:
            return str(value)
    return _settings_value("secret_key")


def _unsubscribe_secrets() -> list[str]:
    """Primary + legacy secrets, for verifying links signed before a rotation."""
    secrets = [_unsubscribe_secret()]
    legacy = _settings_value("unsubscribe_legacy_secrets")
    secrets.extend(part.strip() for part in legacy.replace(";", ",").split(","))
    return [s for s in dict.fromkeys(secrets) if s]


def _token_for(secret: str, campaign_id: str, recipient_id: str) -> str:
    message = f"{campaign_id}:{recipient_id}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return digest[:32]


def build_unsubscribe_token(campaign_id: str, recipient_id: str) -> str:
    return _token_for(_unsubscribe_secret(), str(campaign_id), str(recipient_id))


def verify_unsubscribe_token(campaign_id: str, recipient_id: str, token: str | None) -> bool:
    """Constant-time check; an empty token is always rejected.

    Older deployments accepted unsigned links, which let anyone who knew (or
    guessed) a recipient UUID opt that person out. Only signed links are
    honoured now; links in already-sent mail keep working because the previous
    secret can be listed in ``UNSUBSCRIBE_LEGACY_SECRETS``.
    """
    if not token:
        return False
    supplied = str(token)
    for secret in _unsubscribe_secrets():
        expected = _token_for(secret, str(campaign_id), str(recipient_id))
        if hmac.compare_digest(expected, supplied):
            return True
    return False


def build_unsubscribe_url(base_url: str, campaign_id: str, recipient_id: str) -> str:
    base = base_url.rstrip("/")
    token = build_unsubscribe_token(str(campaign_id), str(recipient_id))
    return (
        f"{base}{_UNSUB_PATH}?cid={quote(str(campaign_id))}"
        f"&rid={quote(str(recipient_id))}&t={token}"
    )


def build_open_pixel_url(base_url: str, campaign_id: str, recipient_id: str) -> str:
    base = base_url.rstrip("/")
    return f"{base}{_TRACK_PATH}{quote(str(campaign_id))}/{quote(str(recipient_id))}/open"


def build_click_signature(campaign_id: str, recipient_id: str, target_url: str) -> str:
    """Sign a click target so the redirector cannot be abused as an open redirect."""
    message = f"click:{campaign_id}:{recipient_id}:{target_url}".encode()
    digest = hmac.new(_unsubscribe_secret().encode(), message, hashlib.sha256).hexdigest()
    return digest[:32]


def verify_click_signature(
    campaign_id: str, recipient_id: str, target_url: str, signature: str | None
) -> bool:
    if not signature:
        return False
    supplied = str(signature)
    for secret in _unsubscribe_secrets():
        message = f"click:{campaign_id}:{recipient_id}:{target_url}".encode()
        expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()[:32]
        if hmac.compare_digest(expected, supplied):
            return True
    return False


def build_click_url(
    base_url: str, campaign_id: str, recipient_id: str, target_url: str
) -> str:
    base = base_url.rstrip("/")
    encoded = quote(target_url, safe="")
    signature = build_click_signature(str(campaign_id), str(recipient_id), target_url)
    return (
        f"{base}{_TRACK_PATH}{quote(str(campaign_id))}/{quote(str(recipient_id))}"
        f"/click?target_url={encoded}&s={signature}"
    )


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


def substitute_merge_tags(
    text: str,
    context: dict[str, Any],
    unsubscribe_url: str,
    *,
    escape_html: bool = False,
) -> str:
    """Replace contact, unsubscribe, metadata and derived merge tags.

    Supports ``{{first_name|there}}`` fallbacks plus the derived
    ``{{greeting_name}}`` (first name, else last name, else "there") and
    ``{{full_name}}`` tags.

    ``escape_html=True`` must be used for HTML bodies: contact fields are
    attacker-controlled data and must not be injected as markup.
    """
    return render_merge_tags(
        text,
        context,
        specials={"unsubscribe": unsubscribe_url, "unsubscribe_url": unsubscribe_url},
        escape_html=escape_html,
    )


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

    processed_html = substitute_merge_tags(
        processed_html, context, unsubscribe_url, escape_html=True
    )
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
    processed_html = substitute_merge_tags(
        processed_html, merged_context, unsubscribe_url, escape_html=True
    )

    processed_text = plain_text
    if processed_text:
        processed_text = substitute_merge_tags(processed_text, merged_context, unsubscribe_url)

    return processed_html, processed_text
