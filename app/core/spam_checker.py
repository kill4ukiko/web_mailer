"""Spam-score analysis and link validation for preflight checks."""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlsplit

import requests

SPAM_TRIGGER_WORDS: tuple[str, ...] = (
    "free", "click here", "act now", "limited time", "urgent", "guaranteed",
    "winner", "prize", "earn money", "double your", "risk-free", "no obligation",
    "100% free", "buy now", "discount", "cash bonus", "credit", "viagra",
    "casino", "lottery", "password", "invest now", "cheap",
)

# Used to short-circuit tracking links during validation.
LINK_CHECK_TIMEOUT = 10
# Cap preflight outbound work so one request cannot fan out to many hosts.
MAX_LINKS_TO_CHECK = 50
MAX_REDIRECTS = 3


@dataclass
class SpamReport:
    score: float = 0.0
    issues: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.score < 3.0:
            return "ok"
        if self.score < 6.0:
            return "warning"
        return "risky"


def scan_content(html_content: str, subject: str, plain_text_content: str | None = None) -> SpamReport:
    """Heuristic spam-content scan. Returns a score between 0 and 10."""
    report = SpamReport()
    text = html_content.lower()
    subject_lower = subject.lower()
    score = 0.0

    if not subject_lower.strip():
        report.issues.append("Missing subject line.")
        score += 1.5

    if plain_text_content is None or not plain_text_content.strip():
        report.issues.append("No plain-text alternative provided.")
        score += 1.0

    if "unsubscribe" not in text:
        report.issues.append("Content has no unsubscribe reference.")
        score += 1.0

    for word in SPAM_TRIGGER_WORDS:
        if word in text or word in subject_lower:
            report.issues.append(f"Spam trigger word/phrase found: '{word}'.")
            score += 0.7

    caps_ratio = _caps_ratio(html_content)
    if caps_ratio > 0.25 and len(html_content) > 200:
        report.issues.append("Excessive ALL-CAPS text detected.")
        score += 1.2

    exclaims = len(re.findall(r"!{2,}", html_content))
    if exclaims > 3:
        report.issues.append("Multiple repeated exclamation marks detected.")
        score += 0.8

    if re.match(r"^\s*<img", html_content, re.IGNORECASE) is not None and "<a " not in html_content.lower():
        report.issues.append("Image-only email with no links (bait-style).")
        score += 0.8

    if "#" in html_content or len(re.findall(r"https?://", html_content.lower())) == 0:
        report.issues.append("Email contains no clickable links.")
        score += 0.5

    report.score = round(min(max(score, 0.0), 10.0), 2)
    return report


def _is_public_host(host: str | None) -> bool:
    """True only when every resolved address is a globally routable IP.

    This blocks the obvious SSRF targets (localhost, RFC1918, link-local cloud
    metadata, etc.). Redirect hops are re-checked too, so a public URL cannot
    bounce the checker into the internal network.
    """
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def is_safe_link(url: str) -> bool:
    """Reject non-HTTP(S) schemes, embedded credentials and internal hosts."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if parts.username or parts.password:
        return False
    return _is_public_host(parts.hostname)


def _request_no_redirects(url: str, method: str):
    """Request ``url`` following redirects manually, validating every hop."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not is_safe_link(current):
            raise requests.RequestException("blocked non-public target")
        resp = requests.request(
            method,
            current,
            timeout=LINK_CHECK_TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": "web-mailer-preflight/1.0"},
        )
        location = resp.headers.get("Location")
        if resp.is_redirect and location:
            current = urljoin(current, location)
            continue
        return resp
    raise requests.RequestException("too many redirects")


def validate_links(urls: Iterable[str]) -> dict[str, str]:
    """HTTP-check each URL; returns {url: status}."""
    results: dict[str, str] = {}
    for url in list(urls)[:MAX_LINKS_TO_CHECK]:
        try:
            resp = _request_no_redirects(url, "HEAD")
            if resp.status_code in (403, 405):
                # Some servers reject HEAD; fall back to GET.
                resp = _request_no_redirects(url, "GET")
            results[url] = "ok" if resp.status_code < 400 else f"error:{resp.status_code}"
        except requests.RequestException as exc:
            results[url] = f"error:{exc.__class__.__name__}"
    return results


def _caps_ratio(text: str) -> float:
    letters = re.findall(r"[A-Za-z]", text)
    if not letters:
        return 0.0
    upper = re.findall(r"[A-Z]", text)
    return len(upper) / len(letters)