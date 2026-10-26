"""RSS parsing and cache behaviour for the public site."""
from __future__ import annotations

from app.core import site_news

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>World</title>
<item>
  <title>Example &amp; headline</title>
  <link>https://example.com/story?utm=1</link>
  <description><![CDATA[<p>A <strong>short</strong> summary.</p>]]></description>
  <pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate>
</item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
</channel></rss>
"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <title>Atom story</title>
  <link href="https://example.org/atom" />
  <summary>Atom summary</summary>
  <updated>2026-10-20T11:00:00Z</updated>
 </entry>
</feed>
"""


def test_parse_rss_extracts_safe_plain_text():
    items = site_news.parse_feed("BBC World", RSS)
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Example & headline"
    assert item["summary"] == "A short summary."
    assert item["source"] == "BBC World"
    assert item["url"].startswith("https://example.com/")
    assert "Oct 2026" in item["published_display"]


def test_parse_atom():
    items = site_news.parse_feed("NPR World", ATOM)
    assert items[0]["title"] == "Atom story"
    assert items[0]["url"] == "https://example.org/atom"
    assert items[0]["published"].startswith("2026-10-20T11:00:00")


def test_parse_rejects_javascript_links():
    assert site_news.parse_feed("Feed", RSS)[0]["url"].startswith("https://")


def test_fetch_failure_is_empty(monkeypatch):
    class Response:
        content = RSS

        def raise_for_status(self):
            raise site_news.requests.RequestException("down")

    monkeypatch.setattr(site_news.requests, "get", lambda *a, **k: Response())
    assert site_news._fetch_feed("BBC", "https://example.com/rss", 2) == []


def test_get_world_news_caches_and_sorts(monkeypatch):
    site_news._MEMORY_CACHE["items"] = []
    site_news._MEMORY_CACHE["fetched_at"] = 0
    calls = []

    def fetch(source, url, timeout):
        calls.append(source)
        return site_news.parse_feed(source, RSS if source == "A" else ATOM)

    monkeypatch.setattr(site_news, "_fetch_feed", fetch)

    class Settings:
        site_news_cache_seconds = 600
        site_news_timeout_seconds = 2
        site_news_feed_list = [("A", "https://a"), ("B", "https://b")]

    first = site_news.get_world_news(Settings())
    second = site_news.get_world_news(Settings())
    assert [item["source"] for item in first] == ["B", "A"]
    assert second == first
    assert sorted(calls) == ["A", "B"]
