"""Unit tests for outbound content processing (pixel, links, unsubscribe)."""
from __future__ import annotations

from app.core.content import (
    append_unsubscribe_footer,
    build_click_url,
    build_open_pixel_url,
    build_unsubscribe_url,
    ensure_email_shell,
    inject_open_pixel,
    render_email,
    render_preview,
    rewrite_links,
    substitute_merge_tags,
)

BASE = "http://localhost:5000"
CID = "camp-1"
RID = "rec-1"


def test_build_urls():
    assert build_open_pixel_url(BASE, CID, RID) == f"{BASE}/api/track/{CID}/{RID}/open"
    assert build_click_url(BASE, CID, RID, "https://e.com/a?x=1") == (
        f"{BASE}/api/track/{CID}/{RID}/click?target_url=https%3A%2F%2Fe.com%2Fa%3Fx%3D1"
    )
    unsubscribe = build_unsubscribe_url(BASE, CID, RID)
    assert unsubscribe.startswith(f"{BASE}/api/unsubscribe?cid={CID}&rid={RID}&t=")
    assert len(unsubscribe.split("&t=")[1]) >= 32


def test_inject_pixel_before_body_close():
    out = inject_open_pixel("<html><body><p>hi</p></body></html>", CID, RID, BASE)
    assert "<img" in out and out.index("<img") < out.index("</body>")
    assert f"/api/track/{CID}/{RID}/open" in out


def test_inject_pixel_appends_without_body():
    out = inject_open_pixel("<p>hi</p>", CID, RID, BASE)
    assert out.endswith(">")
    assert f"/api/track/{CID}/{RID}/open" in out


def test_rewrite_absolute_links_only():
    html = (
        '<a href="https://example.com/page">abs</a>'
        '<a href="http://example.com">http</a>'
        '<a href="/relative">rel</a>'
        '<a href="mailto:x@y.com">mail</a>'
        '<a href="#anchor">anchor</a>'
    )
    out = rewrite_links(html, CID, RID, BASE)
    assert out.count("/api/track/") == 2
    assert 'href="/relative"' in out
    assert "mailto:x@y.com" in out
    assert 'href="#anchor"' in out


def test_rewrite_is_idempotent_and_skips_unsubscribe():
    html = (
        f'<a href="{BASE}/api/track/{CID}/{RID}/click?target_url=https%3A%2F%2Fe.com">x</a>'
        f'<a href="{BASE}/api/unsubscribe?cid={CID}&rid={RID}">u</a>'
    )
    out = rewrite_links(html, CID, RID, BASE)
    assert out == html  # unchanged


def test_merge_tag_substitution():
    ctx = {"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"}
    unsub = build_unsubscribe_url(BASE, CID, RID)
    out = substitute_merge_tags(
        "Hi {{first_name}} {{last_name}} ({{ email }}). Unsub: {{unsubscribe}}", ctx, unsub
    )
    assert "Hi Ada Lovelace (ada@example.com)." in out
    assert unsub in out


def test_render_email_end_to_end():
    html = (
        '<html><body><p>Hi {{first_name}}</p>'
        '<a href="https://example.com">link</a>'
        '<a href="{{unsubscribe}}">unsubscribe</a>'
        "</body></html>"
    )
    out_html, out_text = render_email(
        html,
        "Hi {{first_name}}, unsub {{unsubscribe_url}}",
        CID,
        RID,
        BASE,
        context={"first_name": "Ada"},
    )
    assert "Hi Ada" in out_html
    assert f"{BASE}/api/track/{CID}/{RID}/open" in out_html
    assert "https%3A%2F%2Fexample.com" in out_html
    # unsubscribe link must NOT be wrapped by the click tracker
    assert f"{BASE}/api/unsubscribe?cid={CID}&rid={RID}" in out_html
    assert out_html.count("/api/track/") == 2  # link + pixel only
    assert "Hi Ada" in out_text
    assert f"{BASE}/api/unsubscribe?cid={CID}&rid={RID}" in out_text


def test_ensure_email_shell_wraps_fragment():
    out = ensure_email_shell("<p>hello</p>")
    assert "<html" in out.lower()
    assert "<body" in out.lower()
    assert "<p>hello</p>" in out
    assert out.lower().count("<html") == 1


def test_ensure_email_shell_is_idempotent():
    doc = "<!DOCTYPE html><html><head></head><body><p>x</p></body></html>"
    assert ensure_email_shell(doc) == doc


def test_append_unsubscribe_footer_adds_link():
    html = "<html><body><p>hi</p></body></html>"
    out_html, out_text = append_unsubscribe_footer(html, None)
    assert "{{unsubscribe}}" in out_html
    assert out_html.lower().index("unsubscribe") < out_html.rindex("</body>")
    assert out_text is None


def test_append_unsubscribe_footer_handles_plain_text():
    _, out_text = append_unsubscribe_footer("<p>hi</p>", "plain body")
    assert out_text is not None
    assert "{{unsubscribe}}" in out_text


def test_append_unsubscribe_footer_is_idempotent():
    html = "<html><body><p>hi</p></body></html>"
    once_html, _ = append_unsubscribe_footer(html, None)
    twice_html, _ = append_unsubscribe_footer(once_html, None)
    assert once_html == twice_html


def test_merge_supports_custom_metadata_keys():
    ctx = {"first_name": "Ada", "metadata": {"Company": "Acme Inc"}}
    out = substitute_merge_tags("Welcome {{first_name}} from {{metadata.Company}}!", ctx, "u")
    assert out == "Welcome Ada from Acme Inc!"


def test_merge_unknown_tags_become_empty():
    out = substitute_merge_tags("Hi {{first_name}} {{unknown}}", {"first_name": "Ada"}, "u")
    assert out == "Hi Ada "


def test_render_email_builds_shell_and_footer_for_fragment():
    out_html, _ = render_email("<p>Hi {{first_name}}</p>", None, CID, RID, BASE,
                               context={"first_name": "Ada"})
    assert "<html" in out_html.lower()
    assert "Hi Ada" in out_html


def test_render_preview_has_no_tracking_but_uses_sample_merge():
    out_html, _ = render_preview("<p>Hi {{first_name}}</p>", base_url=BASE)
    assert "Hi Alex" in out_html          # sample merge value
    assert "<html" in out_html.lower()    # shell added
    assert "/api/track/" not in out_html  # no pixel / link rewrite
    assert "unsubscribe" in out_html.lower()


def test_render_preview_accepts_custom_context():
    out_html, _ = render_preview("<p>Hi {{first_name}}</p>",
                                 context={"first_name": "Grace"}, base_url=BASE)
    assert "Hi Grace" in out_html
