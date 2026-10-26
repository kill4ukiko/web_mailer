"""Cached world-news feed for the public GoPriLad home page."""
from __future__ import annotations

import html
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

from app.config import Settings, get_settings
from app.extensions import _RedisHolder

logger = logging.getLogger(__name__)

CACHE_KEY = "site:news:world:v1"
_CACHE_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, object] = {"items": [], "fetched_at": 0.0}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return " ".join(" ".join(self.parts).split())


def strip_html(value: str | None) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:  # noqa: BLE001 - malformed feed markup
        return ""
    return parser.text()


def _clean(text: str | None, limit: int) -> str:
    cleaned = " ".join(html.unescape(str(text or "")).split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _valid_url(url: str | None) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in node:
        if _local_name(child.tag) in wanted:
            return "".join(child.itertext())
    return ""


def _link(node: ET.Element) -> str:
    for child in node:
        if _local_name(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href.strip()
        if child.text and child.text.strip():
            return child.text.strip()
    guid = _child_text(node, "guid", "id")
    return guid if _valid_url(guid) else ""


def _published(node: ET.Element) -> datetime | None:
    raw = _child_text(node, "pubdate", "published", "updated", "date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_feed(source: str, content: bytes | str) -> list[dict]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        logger.warning("site news feed parse failed for %s", source)
        return []
    nodes = [node for node in root.iter() if _local_name(node.tag) in ("item", "entry")]
    items: list[dict] = []
    for node in nodes:
        title = _clean(_child_text(node, "title"), 200)
        url = _link(node)
        if not title or not _valid_url(url):
            continue
        summary = strip_html(_child_text(node, "description", "summary", "content"))
        published = _published(node)
        items.append(
            {
                "title": title,
                "url": url,
                "summary": _clean(summary, 260),
                "source": source,
                "published": published.isoformat() if published else "",
                "published_display": published.strftime("%d %b %Y, %H:%M UTC")
                if published
                else "",
            }
        )
    return items


def _fetch_feed(source: str, url: str, timeout: int) -> list[dict]:
    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "GoPriLad/1.0 (+https://goprilad.com)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
            },
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("site news fetch failed for %s: %s", source, exc)
        return []
    return parse_feed(source, response.content)


def _read_cache(settings: Settings) -> list[dict] | None:
    try:
        raw = _RedisHolder.get().get(CACHE_KEY)
    except Exception:  # noqa: BLE001 - Redis is optional for the public page
        raw = None
    if raw:
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return items
        except (TypeError, ValueError):
            pass
    age = time.time() - float(_MEMORY_CACHE.get("fetched_at") or 0)
    if age < max(1, settings.site_news_cache_seconds):
        cached = _MEMORY_CACHE.get("items")
        if isinstance(cached, list):
            return cached
    return None


def _write_cache(settings: Settings, items: list[dict]) -> None:
    _MEMORY_CACHE["items"] = items
    _MEMORY_CACHE["fetched_at"] = time.time()
    try:
        _RedisHolder.get().setex(
            CACHE_KEY, max(60, settings.site_news_cache_seconds), json.dumps(items)
        )
    except Exception:  # noqa: BLE001 - in-memory fallback still works
        logger.debug("site news Redis cache write failed", exc_info=True)


def get_world_news(settings: Settings | None = None) -> list[dict]:
    settings = settings or get_settings()
    cached = _read_cache(settings)
    if cached is not None:
        return cached
    with _CACHE_LOCK:
        cached = _read_cache(settings)
        if cached is not None:
            return cached
        feeds = settings.site_news_feed_list
        items: list[dict] = []
        if feeds:
            with ThreadPoolExecutor(max_workers=min(4, len(feeds))) as pool:
                futures = [
                    pool.submit(
                        _fetch_feed,
                        source,
                        url,
                        settings.site_news_timeout_seconds,
                    )
                    for source, url in feeds
                ]
                for future in as_completed(futures):
                    try:
                        items.extend(future.result())
                    except Exception:  # noqa: BLE001 - one bad feed must not hide the rest
                        logger.warning("site news feed worker failed", exc_info=True)
        seen: set[str] = set()
        unique: list[dict] = []
        for item in sorted(items, key=lambda row: row.get("published", ""), reverse=True):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            unique.append(item)
        unique = unique[:24]
        if unique:
            _write_cache(settings, unique)
        else:
            stale = _MEMORY_CACHE.get("items")
            if isinstance(stale, list):
                return stale
        return unique
