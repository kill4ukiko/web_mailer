"""Per-section Telegram overrides: resolution, masking, and settings UI."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core import telegram


@pytest.fixture(autouse=True)
def _clear_telegram_cache():
    telegram.clear_cache()
    yield
    telegram.clear_cache()


def _env(monkeypatch, token="", chat=""):
    monkeypatch.setattr(
        telegram,
        "get_settings",
        lambda: SimpleNamespace(telegram_bot_token=token, telegram_chat_id=chat),
    )


def test_db_overrides_win_per_field(monkeypatch, supabase):
    _env(monkeypatch, token="env-token", chat="env-chat")
    supabase.sms_settings_row = {
        "id": "s1",
        "telegram_bot_token": "db-token",
        "telegram_chat_id": "",
    }

    config = telegram.resolve_telegram("b_sms", use_cache=False)

    assert config.token == "db-token"
    assert config.chat_id == "env-chat"
    assert config.token_source == "db"
    assert config.chat_source == "env"


def test_sections_resolve_independently(monkeypatch, supabase):
    _env(monkeypatch, token="env-token", chat="env-chat")
    supabase.settings_row = {
        "id": "e1",
        "telegram_bot_token": "email-token",
        "telegram_chat_id": "email-chat",
    }
    supabase.sms_settings_row = {
        "id": "s1",
        "telegram_bot_token": "",
        "telegram_chat_id": "sms-chat",
    }

    email = telegram.resolve_telegram("email", use_cache=False)
    b_sms = telegram.resolve_telegram("b_sms", use_cache=False)
    bird = telegram.resolve_telegram("bird_sms", use_cache=False)

    assert (email.token, email.chat_id) == ("email-token", "email-chat")
    assert (b_sms.token, b_sms.chat_id) == ("env-token", "sms-chat")
    assert (bird.token, bird.chat_id) == ("env-token", "env-chat")


def test_public_state_never_exposes_token(monkeypatch, supabase):
    _env(monkeypatch)
    supabase.bird_sms_settings_row = {
        "id": "b1",
        "telegram_bot_token": "secret-bird-token",
        "telegram_chat_id": "bird-chat",
    }

    state = telegram.telegram_public_state("bird_sms")

    assert "telegram_bot_token" not in state
    assert state["telegram_token_set"] is True
    assert state["telegram_token_hint"] == "****oken"
    assert state["telegram_configured"] is True


def test_settings_apis_return_section_state_and_mask_token(client, monkeypatch, supabase):
    _env(monkeypatch)
    supabase.settings_row = {
        "id": "e1",
        "telegram_bot_token": "email-secret",
        "telegram_chat_id": "email-chat",
    }
    supabase.sms_settings_row = {
        "id": "s1",
        "telegram_chat_id": "sms-chat",
    }

    email = client.get("/api/settings").get_json()["settings"]
    sms = client.get("/api/sms/settings").get_json()["settings"]

    assert email["telegram_chat_id"] == "email-chat"
    assert email["telegram_token_hint"] == "****cret"
    assert "telegram_bot_token" not in email
    assert sms["telegram_chat_id"] == "sms-chat"
    assert sms["telegram_token_set"] is False


def test_patch_clears_override_and_invalidates_cache(client, monkeypatch, supabase):
    _env(monkeypatch, token="env-token", chat="env-chat")
    supabase.settings_row = {
        "id": "e1",
        "telegram_bot_token": "db-token",
        "telegram_chat_id": "db-chat",
    }
    assert telegram.resolve_telegram("email").token == "db-token"

    resp = client.patch("/api/settings", json={
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    })

    assert resp.status_code == 200, resp.get_json()
    config = telegram.resolve_telegram("email", use_cache=False)
    assert (config.token, config.chat_id) == ("env-token", "env-chat")
    assert config.token_source == "env"
    assert config.chat_source == "env"


def test_pages_render_telegram_override_fields(client, supabase):
    supabase.settings_row = {"id": "e1", "telegram_chat_id": "email-chat"}
    supabase.sms_settings_row = {"id": "s1", "telegram_chat_id": "sms-chat"}

    email = client.get("/settings")
    sms = client.get("/sms/settings")

    assert email.status_code == 200
    assert sms.status_code == 200
    assert b'name="telegram_bot_token"' in email.data
    assert b'id="clear-telegram-btn"' in email.data
    assert b"email-chat" in email.data
    assert b'name="telegram_bot_token"' in sms.data
    assert b'id="clear-sms-telegram-btn"' in sms.data
    assert b"sms-chat" in sms.data

    supabase.bird_sms_settings_row = {
        "id": "b1",
        "bird_api_key": "bk_super-secret",
        "telegram_chat_id": "bird-chat",
    }
    bird = client.get("/bird-sms/settings")
    assert bird.status_code == 200
    assert b'name="telegram_bot_token"' in bird.data
    assert b'id="bird-telegram-clear-btn"' in bird.data
    assert b"bird-chat" in bird.data
    assert b"bk_super-secret" not in bird.data
