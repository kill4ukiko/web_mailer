"""Inspect the Telegram bot and switch it between webhook and polling modes.

Telegram delivers updates either to a webhook *or* to ``getUpdates`` pollers,
never both. This app's ``/report`` and ``/restart`` commands use the built-in
poller, so a bot that is already attached to another project's webhook must
either get a dedicated token or have its webhook removed.

Usage::

    python -m scripts.telegram_bot info
    python -m scripts.telegram_bot chats
    python -m scripts.telegram_bot use-polling --yes

``chats`` lists chats that recently messaged the bot, which is the easiest way
to find the numeric ``TELEGRAM_CHAT_ID`` for a new bot.

``use-polling`` calls ``deleteWebhook`` and is therefore explicit: it takes the
bot away from any other service currently receiving its updates.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

import requests

from app.core.telegram import resolve_telegram


def _call(token: str, method: str, **params) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json=params or None, timeout=20)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"ok": False, "description": resp.text[:300]}
    if not payload.get("ok"):
        raise RuntimeError(f"{method} failed [{resp.status_code}]: {payload.get('description')}")
    return payload.get("result") or {}



def _config():
    config = resolve_telegram("email", use_cache=False)
    if not config.configured:
        raise SystemExit(
            "Telegram is not configured. Set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
            "or fill them in Settings."
        )
    return config


def cmd_info(_args) -> int:
    config = _config()
    me = _call(config.token, "getMe")
    hook = _call(config.token, "getWebhookInfo")
    print(f"bot: @{me.get('username')} ({me.get('first_name')})")
    print(f"token source: {config.token_source}")
    print(f"chat: {config.chat_id} (source: {config.chat_source})")
    if hook.get("url"):
        print(f"webhook: {hook['url']} (pending updates: {hook.get('pending_update_count', 0)})")
        print("commands: DISABLED until the webhook is removed or a dedicated token is used")
    else:
        print("webhook: none — the /report and /restart poller can receive updates")
    return 0


def cmd_chats(_args) -> int:
    config = _config()
    updates = _call(config.token, "getUpdates", timeout=0, limit=100)
    if not isinstance(updates, list):
        updates = []
    chats: dict[str, str] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        label = (
            chat.get("title")
            or chat.get("username")
            or " ".join(
                part for part in (chat.get("first_name"), chat.get("last_name")) if part
            )
            or chat.get("type")
            or ""
        )
        chats[str(chat_id)] = label
    if not chats:
        print("No chats found. Send /start (or any message) to the bot, then retry.")
        return 0
    for chat_id, label in chats.items():
        print(f"{chat_id}\t{label}")
    return 0


def cmd_use_polling(args) -> int:
    config = _config()
    hook = _call(config.token, "getWebhookInfo")
    current = hook.get("url")
    if not current:
        print("webhook: none — polling already works")
        return 0
    if not args.yes:
        print(f"A webhook is active: {current}", file=sys.stderr)
        print("Re-run with --yes to remove it and let this app poll for commands.",
              file=sys.stderr)
        return 1
    _call(config.token, "deleteWebhook", drop_pending_updates=False)
    print(f"removed webhook: {current}")
    print("The /report and /restart poller can now receive updates.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    info = sub.add_parser("info", help="show bot identity and webhook status")
    info.set_defaults(func=cmd_info)
    chats = sub.add_parser("chats", help="list chats that messaged the bot (find chat id)")
    chats.set_defaults(func=cmd_chats)
    polling = sub.add_parser("use-polling", help="delete the webhook so this app can poll")
    polling.add_argument("--yes", action="store_true", help="confirm webhook removal")
    polling.set_defaults(func=cmd_use_polling)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as exc:
        print(f"Telegram error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
