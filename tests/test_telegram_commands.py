"""Telegram operator commands (/report, /restart) and the update poller."""
from __future__ import annotations

from unittest import mock


def _campaign(supabase, *, status="paused", name="Launch"):
    campaign = supabase.create_campaign({
        "name": name, "subject": "s", "from_email": "n@x.co",
        "from_name": "n", "html_content": "<p>x</p>",
        "status": status,
    })
    campaign["stats"] = {"sent": 3, "delivered": 2, "hard_bounce": 1, "bounces": 1}
    return campaign


def _recipient(supabase, campaign, email, status, **extra):
    contact = supabase.create_contact({"email": email})[0]
    supabase.add_recipients(campaign["id"], [{"email": email}])
    row = next(r for r in supabase.recipients if r["contact_id"] == contact["id"])
    row.update({"status": status, **extra})
    return row


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_command_strips_bot_suffix_and_argument():
    from app.core.telegram_commands import parse_command

    assert parse_command("/report Launch") == ("report", "Launch")
    assert parse_command("/restart@GoPriLadBot Summer sale") == ("restart", "Summer sale")
    assert parse_command("  /help  ") == ("help", "")
    assert parse_command("hello") is None
    assert parse_command("/") is None


def test_clean_argument_removes_matching_quotes():
    from app.core.telegram_commands import clean_argument

    assert clean_argument('"Summer sale"') == "Summer sale"
    assert clean_argument("'Launch'") == "Launch"
    assert clean_argument(" plain ") == "plain"


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
def test_unknown_and_non_command_messages_are_ignored(supabase):
    from app.core.telegram_commands import handle_text

    assert handle_text("good morning") is None
    assert handle_text("/deploy now") is None


def test_report_command_returns_campaign_report(supabase):
    from app.core.telegram_commands import handle_text

    campaign = _campaign(supabase)
    _recipient(supabase, campaign, "one@x.co", "delivered")
    _recipient(supabase, campaign, "two@x.co", "hard_bounce")
    _recipient(supabase, campaign, "three@x.co", "sent")

    reply = handle_text("/report Launch")
    assert "📊" in reply
    assert "Launch" in reply
    assert "Status: *paused*" in reply
    assert "Sent: 3 / 3" in reply


def test_report_command_handles_unknown_campaign(supabase):
    from app.core.telegram_commands import handle_text

    reply = handle_text("/report Nope")
    assert "No campaign named" in reply
    assert "/report <campaign name>" in reply


def test_restart_command_continues_from_queue(supabase, monkeypatch):
    from app.core.telegram_commands import handle_text

    fake = mock.Mock()
    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", fake)
    campaign = _campaign(supabase)
    _recipient(supabase, campaign, "done@x.co", "delivered",
               sent_at="2026-01-01T01:00:00Z")
    _recipient(supabase, campaign, "bad@x.co", "hard_bounce")
    _recipient(supabase, campaign, "next@x.co", "queued")

    reply = handle_text("/restart Launch")

    assert "Campaign restarted" in reply
    assert "Queued remaining: 1" in reply
    assert supabase.get_campaign(campaign["id"])["status"] == "sending"
    fake.delay.assert_called_once_with(campaign["id"])


def test_restart_command_on_completed_campaign_returns_report(supabase):
    from app.core.telegram_commands import handle_text

    campaign = _campaign(supabase, status="completed", name="Done")
    campaign["finished_at"] = "2026-01-01T00:00:00Z"

    reply = handle_text("/restart Done")

    assert "📊" in reply
    assert "Sending completed" in reply
    assert supabase.get_campaign(campaign["id"])["status"] == "completed"


def test_restart_command_rejects_sending_campaign(supabase):
    from app.core.telegram_commands import handle_text

    _campaign(supabase, status="sending", name="Live")
    reply = handle_text("/restart Live")
    assert "not paused" in reply


def test_restart_command_honours_kill_switch(supabase, monkeypatch):
    from app.core.telegram_commands import handle_text

    fake = mock.Mock()
    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", fake)
    supabase.settings_row = {"id": "s1", "sending_paused": True}
    campaign = _campaign(supabase)

    reply = handle_text("/restart Launch")

    assert "kill switch" in reply
    assert supabase.get_campaign(campaign["id"])["status"] == "paused"
    fake.delay.assert_not_called()


def test_restart_command_requires_exact_name_when_ambiguous(supabase, monkeypatch):
    from app.core.telegram_commands import handle_text

    monkeypatch.setattr("app.workers.tasks_campaign.send_campaign", mock.Mock())
    _campaign(supabase, name="Launch A")
    _campaign(supabase, name="Launch B")

    reply = handle_text("/restart Launch")
    assert "More than one campaign matches" in reply


def test_help_lists_commands():
    from app.core.telegram_commands import handle_text

    reply = handle_text("/help")
    assert "/report" in reply
    assert "/restart" in reply


# --------------------------------------------------------------------------- #
# Poller
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Bot maintenance script
# --------------------------------------------------------------------------- #
def test_bot_info_reports_webhook_conflict(monkeypatch, capsys):
    from app.core.telegram import TelegramConfig
    from scripts import telegram_bot

    monkeypatch.setattr(
        telegram_bot, "_config",
        lambda: TelegramConfig(token="t", chat_id="42"),
    )
    monkeypatch.setattr(
        telegram_bot, "_call",
        lambda token, method, **p: (
            {"username": "bot", "first_name": "Bot"}
            if method == "getMe"
            else {"url": "https://other.example/hook", "pending_update_count": 0}
        ),
    )

    assert telegram_bot.main(["info"]) == 0
    out = capsys.readouterr().out
    assert "https://other.example/hook" in out
    assert "DISABLED" in out


def test_bot_use_polling_requires_confirmation(monkeypatch, capsys):
    from app.core.telegram import TelegramConfig
    from scripts import telegram_bot

    deleted: list[str] = []
    monkeypatch.setattr(
        telegram_bot, "_config",
        lambda: TelegramConfig(token="t", chat_id="42"),
    )
    monkeypatch.setattr(
        telegram_bot, "_call",
        lambda token, method, **p: (
            {"url": "https://other.example/hook"}
            if method == "getWebhookInfo"
            else deleted.append(method) or {}
        ),
    )

    assert telegram_bot.main(["use-polling"]) == 1
    assert deleted == []
    assert "Re-run with --yes" in capsys.readouterr().err


def test_poll_once_replies_only_in_the_configured_chat(supabase, monkeypatch):
    from app.core import telegram as telegram_module
    from app.core import telegram_commands

    _campaign(supabase, name="Launch")
    sent: list[str] = []
    offsets: list[int] = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def get_updates(self, offset=None, timeout=0):
            return [
                {"update_id": 10, "message": {
                    "chat": {"id": 42}, "text": "/report Launch"}},
                {"update_id": 11, "message": {
                    "chat": {"id": 999}, "text": "/restart Launch"}},
                {"update_id": 12, "message": {"chat": {"id": 42}}},
            ]

        def send_message(self, text):
            sent.append(text)
            return True

    monkeypatch.setattr(
        telegram_module, "resolve_telegram",
        lambda *a, **k: telegram_module.TelegramConfig(token="t", chat_id="42"),
    )
    monkeypatch.setattr(telegram_module, "TelegramClient", FakeClient)
    monkeypatch.setattr(telegram_commands, "_read_offset", lambda: 10)
    monkeypatch.setattr(telegram_commands, "_write_offset", offsets.append)

    result = telegram_commands.poll_once()

    assert result == {"processed": 1, "replied": 1}
    assert len(sent) == 1 and "Launch" in sent[0]
    # The foreign chat was acknowledged but never answered.
    assert offsets == [13]
    assert supabase.get_campaign(supabase.campaigns[0]["id"])["status"] == "paused"
