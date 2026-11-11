"""
Print recent Telegram updates to help pick the right TELEGRAM_CHAT_ID.

Usage:
    1. In Telegram, open your bot and send it any message (e.g. "hi").
    2. Run: python manage.py telegram_chatid
    3. Copy the `chat.id` of your own message into .env and restart.

Also sends a test ping once the id is configured (via --test-send).
"""

from __future__ import annotations

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand

from core.services.telegram import notify


class Command(BaseCommand):
    help = "Diagnose Telegram bot → user config: list recent updates, send a test."

    def add_arguments(self, parser):
        parser.add_argument("--test-send", action="store_true", help="Send a test message to the configured chat.")

    def handle(self, *args, test_send=False, **opts):
        token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""
        if not token:
            self.stdout.write(self.style.ERROR("TELEGRAM_BOT_TOKEN is empty."))
            return

        if test_send:
            ok = notify("🧪 khidr test message.")
            self.stdout.write(self.style.SUCCESS("sent") if ok else self.style.ERROR("send failed — check logs"))
            return

        try:
            r = httpx.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.ERROR(f"request failed: {exc}"))
            return

        if r.status_code != 200:
            self.stdout.write(self.style.ERROR(f"HTTP {r.status_code}: {r.text[:200]}"))
            return

        data = r.json()
        updates = data.get("result", [])
        if not updates:
            self.stdout.write(self.style.WARNING(
                "No updates. Open your bot in Telegram and send it a message "
                "(e.g. 'hi'), then run this command again."
            ))
            return

        seen: dict[int, str] = {}
        for upd in updates:
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            name = chat.get("username") or chat.get("first_name") or str(cid)
            if cid is not None and cid not in seen:
                seen[cid] = name

        self.stdout.write(self.style.NOTICE("Recent chat_id values seen:"))
        for cid, name in seen.items():
            self.stdout.write(f"  {cid}   ({name})")
        self.stdout.write("")
        self.stdout.write("Copy the one that's YOU (not the bot) into .env:")
        self.stdout.write(f"  TELEGRAM_CHAT_ID={next(iter(seen))}")
