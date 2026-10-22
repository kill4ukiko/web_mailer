"""Static checks for the in-app help registry used by every template."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
APP_JS = ROOT / "app" / "static" / "app.js"


def _info_keys() -> set[str]:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const INFO = {")
    end = source.index("\n};", start)
    block = source[start:end]
    keys = re.findall(r"^  ([a-z0-9_]+): \{", block, re.MULTILINE)
    assert len(keys) == len(set(keys)), "duplicate INFO keys found"
    return set(keys)


def test_every_data_info_key_has_an_info_entry():
    keys = _info_keys()
    used: dict[str, list[str]] = {}
    for template in TEMPLATES.rglob("*.html"):
        text = template.read_text(encoding="utf-8-sig")
        for key in re.findall(r'data-info="([a-z0-9_]+)"', text):
            used.setdefault(key, []).append(str(template.relative_to(ROOT)))

    missing = {key: paths for key, paths in used.items() if key not in keys}
    assert not missing, f"data-info keys without INFO entries: {missing}"


def test_bird_pages_have_broad_help_coverage():
    minimums = {
        "index.html": 15,
        "contacts.html": 18,
        "templates.html": 12,
        "campaign_form.html": 25,
        "campaign_detail.html": 12,
        "campaign_report.html": 5,
        "settings.html": 30,
    }
    for name, minimum in minimums.items():
        text = (TEMPLATES / "bird" / name).read_text(encoding="utf-8-sig")
        count = len(re.findall(r'data-info="[a-z0-9_]+"', text))
        assert count >= minimum, f"{name} has only {count} help icons"


def test_bird_management_key_fallback_is_documented():
    source = APP_JS.read_text(encoding="utf-8")
    assert "reuses <code>BIRD_API_KEY</code>" in source
    settings = (TEMPLATES / "bird" / "settings.html").read_text(encoding="utf-8-sig")
    assert "Blank reuses the main Bird key" in settings
