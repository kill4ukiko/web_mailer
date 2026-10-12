"""Spam-score analysis and link validation for preflight checks."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

import requests

SPAM_TRIGGER_WORDS: tuple[str, ...] = (
    "free", "click here", "act now", "limited time", "urgent", "guaranteed",
    "winner", "prize", "earn money", "double your", "risk-free", "no obligation",
    "100% free", "buy now", "discount", "cash bonus", "credit", "viagra",
    "casino", "lottery", "password", "invest now", "cheap",
)

# Used to short-circuit tracking links during validation.
LINK_CHECK_TIMEOUT = 10


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


def validate_links(urls: Iterable[str]) -> dict[str, str]:
    """HTTP-check each URL; returns {url: status}."""
    results: dict[str, str] = {}
    for url in urls:
        try:
            resp = requests.request(
                "HEAD", url, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True,
                headers={"User-Agent": "web-mailer-preflight/1.0"},
            )
            if resp.status_code >= 400 and resp.status_code in (405, 403):
                # Some servers reject HEAD; fall back to GET.
                resp = requests.request(
                    "GET", url, timeout=LINK_CHECK_TIMEOUT, allow_redirects=True,
                    headers={"User-Agent": "web-mailer-preflight/1.0"},
                )
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