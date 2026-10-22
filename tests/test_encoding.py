"""Encoding guards for the source tree and rendered pages.

Two classes of bug this pins down:

* A UTF-8 BOM at the start of a template ends up *before* ``<!doctype html>``
  in the response, which makes browsers render the page in quirks mode (the
  "B SMS page flashes unstyled" report: SMS pages stacked three BOMs).
* cp1252-decoded UTF-8 ("mojibake") turns the Telegram alert emoji into
  ASCII garbage and em dashes into stray accented characters.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "env", ".tools", "__pycache__", "node_modules", ".pytest_cache"}
SOURCE_EXTS = {
    ".py", ".html", ".js", ".css", ".md", ".sql", ".json", ".yml", ".yaml",
    ".txt", ".bat", ".cfg", ".ini", ".ps1", ".example", ".toml",
}

# Substrings that only occur when UTF-8 bytes were decoded as cp1252/latin-1.
# Written with escapes so this file itself stays clean.
MOJIBAKE = (
    "\u00e2\u20ac",  # em dash / smart quotes
    "\u00e2\u201e",  # superscript / trademark
    "\u00e2\u0153",  # check mark
    "\u00e2\u0161",  # warning sign
    "\u00c3\u00a9",  # e-acute
    "\u00c3\u00a8",  # e-grave
    "\u00c2\u00b7",  # middle dot
    "\u00c2\u00bb",  # right chevron
    "\u00c2\u00ab",  # left chevron
    "\u00f0\u0178",  # emoji prefix
    "\u00ef\u00b8",  # variation selector
)


def _source_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SOURCE_EXTS:
            yield path


def test_no_utf8_bom():
    offenders = [
        str(path.relative_to(ROOT))
        for path in _source_files()
        if path.read_bytes().startswith(b"\xef\xbb\xbf")
    ]
    assert not offenders, f"UTF-8 BOM found in: {offenders}"


def test_no_mojibake():
    offenders = []
    for path in _source_files():
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            offenders.append(f"{path.relative_to(ROOT)} (not valid UTF-8)")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(marker in line for marker in MOJIBAKE):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, f"mojibake found in: {offenders}"


def test_rendered_pages_start_with_doctype(client, supabase):
    from tests.test_sms_web import _seed

    _seed(supabase)
    for path in ("/", "/email", "/sms", "/bird-sms", "/sms/contacts",
                 "/bird-sms/contacts", "/settings"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.data.startswith(b"<!doctype html>"), (
            f"{path} does not start with <!doctype html> (got {resp.data[:24]!r});"
            " a leading BOM puts the page in quirks mode"
        )


def test_static_assets_are_cache_busted(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"style.css?v=" in resp.data
