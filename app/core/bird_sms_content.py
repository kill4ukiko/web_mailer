"""Bird SMS content helpers (segment estimate + 12-segment provider cap)."""
from __future__ import annotations

from app.config import BIRD_MAX_SEGMENTS
from app.core.sms_content import PREVIEW_CONTEXT, is_gsm7, normalise_phone, render_sms


class BirdContentError(ValueError):
    pass


def render_bird_sms(content: str, context: dict | None = None, *,
                    include_stop: bool = False, smart_encoding: bool = True) -> tuple[str, int]:
    """Render merge tags and return ``(body, segments)``.

    Bird accepts at most 12 segments for free-text messages. We reject longer
    bodies locally instead of burning an API call that Bird would reject.
    """
    unicode_enabled = None if smart_encoding else True
    body, segments = render_sms(
        content, context or PREVIEW_CONTEXT, include_stop=include_stop,
        unicode_enabled=unicode_enabled,
    )
    if segments > BIRD_MAX_SEGMENTS:
        raise BirdContentError(
            f"message is {segments} segments; Bird's limit is {BIRD_MAX_SEGMENTS}"
        )
    return body, segments


def estimate_segments(body: str, smart_encoding: bool = True) -> int:
    if not body:
        return 0
    unicode_enabled = (not is_gsm7(body)) if smart_encoding else True
    single, multi = (70, 67) if unicode_enabled else (160, 153)
    return 1 if len(body) <= single else (len(body) + multi - 1) // multi


__all__ = [
    "BirdContentError", "PREVIEW_CONTEXT", "render_bird_sms", "estimate_segments",
    "normalise_phone",
]
