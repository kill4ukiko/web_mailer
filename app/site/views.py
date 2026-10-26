"""Public GoPriLad pages."""
from __future__ import annotations

import logging

from flask import Response, render_template, url_for

from app.config import get_settings
from app.core.site_news import get_world_news
from app.site import bp

logger = logging.getLogger(__name__)


def home_page():
    try:
        news = get_world_news()
    except Exception:  # noqa: BLE001 - never take the home page down for a feed
        logger.warning("site news unavailable", exc_info=True)
        news = []
    return render_template("site/home.html", news=news, feed_error=not news)


def about_page():
    return render_template("site/about.html")


@bp.get("/contact")
def contact_page():
    return render_template("site/contact.html")


@bp.get("/privacy")
def privacy_page():
    return render_template("site/privacy.html")


@bp.get("/terms")
def terms_page():
    return render_template("site/terms.html")


@bp.get("/robots.txt")
def robots():
    base = str(get_settings().site_base_url or "").rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /dashboard\n"
        "Disallow: /verify-email\n"
        "Disallow: /login\n"
        f"\nSitemap: {base}{url_for('site.sitemap')}\n"
    )
    return Response(body, mimetype="text/plain")


@bp.get("/sitemap.xml")
def sitemap():
    base = str(get_settings().site_base_url or "").rstrip("/")
    paths = [
        url_for("web.home"),
        url_for("web.about_page"),
        url_for("site.register_page"),
        url_for("site.contact_page"),
        url_for("site.privacy_page"),
        url_for("site.terms_page"),
    ]
    entries = "".join(f"<url><loc>{base}{path}</loc></url>" for path in paths)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{entries}</urlset>"
    )
    return Response(xml, mimetype="application/xml")
