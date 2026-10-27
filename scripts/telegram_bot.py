"""Inspect the Telegram bot and switch it between webhook and polling modes.

Telegram delivers updates either to a webhook *or* to ``getUpdates`` pollers,
never both. This app's ``/report`` and ``/restart`` commands use the built-in
poller, so a bot that is already attached to another project's webhook must
either get a dedicated token or have its webhook removed.

Usage::

    python -m scripts.telegram_bot info
    python -m scripts.telegram_bot use-polling --yes

``use-polling`` calls ``deleteWebhook`` and is therefore explicit: it takes the
bot away from any other service currently receiving its updates.
"""
from __future__ import annotations

import argparse
import sys

import requests

from app.core.telegram import resolve_telegram


def _call(token: str, method: str, **params) -> dict:
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
    print(f"chat: {config.chat_id} (source: {config.chat_source})")
    if hook.get("url"):
        print(f"webhook: {hook['url']} (pending updates: {hook.get('pending_update_count', 0)})")
        print("commands: DISABLED until the webhook is removed or a dedicated token is used")
    else:
        print("webhook: none — the /report and /restart poller can receive updates")
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
    polling = sub.add_parser("use-polling", help="delete the webhook so this app can poll")
    polling.add_argument("--yes", action="store_true", help="confirm webhook removal")
    polling.set_defaults(func=cmd_use_polling)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
