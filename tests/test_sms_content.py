"""SMS content helpers: phone/sender validation, segments, merge tags."""
from __future__ import annotations

from app.core import sms_content as sc


# ---------------------------------------------------------------- phone numbers
def test_normalise_phone_international_and_national():
    assert sc.normalise_phone("+1 555 123 4567") == "+15551234567"
    assert sc.normalise_phone("(555) 123-4567", "1") == "+15551234567"
    assert sc.normalise_phone("07551234567", "44") == "+447551234567"
    assert sc.normalise_phone("0044 7551234567") == "+447551234567"


def test_normalise_phone_rejects_junk_and_missing_cc():
    assert sc.normalise_phone("") is None
    assert sc.normalise_phone("abc") is None
    # A national number without a default country code cannot be promoted.
    assert sc.normalise_phone("07551234567") is None
    assert sc.normalise_phone("+0123") is None
    assert sc.normalise_phone("+1") is None


def test_is_valid_phone():
    assert sc.is_valid_phone("+15551234567") is True
    assert sc.is_valid_phone("5551234567") is False


# ------------------------------------------------------------------- sender IDs
def test_validate_sender_alphanumeric():
    assert sc.validate_sender("MyBrand") is None
    assert sc.validate_sender("") is not None
    assert sc.validate_sender("TooLongBrandName") is not None  # > 11
    assert sc.validate_sender("Bad-Name") is not None  # no punctuation


def test_validate_sender_numeric():
    assert sc.validate_sender("15551234567") is None
    assert sc.validate_sender("123") is None
    assert sc.validate_sender("12") is not None
    assert sc.validate_sender("1" * 16) is not None  # > 15 digits


# --------------------------------------------------------------------- segments
def test_segments_gsm_single_and_multi():
    assert sc.sms_segments("hello") == 1
    assert sc.sms_segments("a" * 160) == 1
    assert sc.sms_segments("a" * 161) == 2
    assert sc.sms_segments("a" * (160 + 153)) == 2
    assert sc.sms_segments("a" * (160 + 154)) == 3


def test_segments_extended_chars_cost_two():
    # A single '€' (extended) occupies two septets.
    assert sc.gsm7_units("€") == 2
    assert sc.sms_segments("€" * 80) == 1
    assert sc.sms_segments("€" * 81) == 2


def test_segments_unicode():
    # A non-GSM char forces UCS-2: 70 per single, 67 per concatenated.
    assert sc.is_gsm7("Hello") is True
    assert sc.is_gsm7("Привет") is False
    assert sc.sms_segments("П" * 70) == 1
    assert sc.sms_segments("П" * 71) == 2
    # An emoji is one code point but two UTF-16 units.
    assert sc.sms_segments("😀" * 35) == 1
    assert sc.sms_segments("😀" * 36) == 2


def test_segments_forced_unicode_flag():
    # Forcing unicode on a short GSM body still counts as one segment here.
    assert sc.sms_segments("hello", unicode_enabled=True) == 1


# ------------------------------------------------------------------ merge tags
def test_render_sms_substitutes_merge_tags():
    body, segments = sc.render_sms(
        "Hi {{first_name}} {{last_name}}, call {{phone}}",
        {"first_name": "Alex", "last_name": "Doe", "phone": "+15551234567"},
    )
    assert body == "Hi Alex Doe, call +15551234567"
    assert segments == 1


def test_render_sms_metadata_and_missing_tags():
    body, _ = sc.render_sms(
        "{{metadata.company}} {{unknown}}",
        {"metadata": {"company": "Acme"}},
    )
    assert body == "Acme "


def test_render_sms_stop_footer_only_once():
    body, _ = sc.render_sms("Sale now!", include_stop=True)
    assert "Reply STOP to opt out." in body
    again, _ = sc.render_sms(body, include_stop=True)
    assert again.count("Reply STOP to opt out.") == 1


def test_unsubscribe_tag_maps_to_stop():
    body, _ = sc.render_sms("Unsub: {{unsubscribe}}", {})
    assert body == "Unsub: STOP"
