"""One-way Telegram notifier (orchestrator → user).

Reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from settings. If either
is empty the notifier is a no-op — safe to call unconditionally. Network
errors are swallowed and logged; the calling code is never blocked.
"""

from __future__ import annotations

import logging

import httpx
from django.conf import settings

log = logging.getLogger(__name__)


def notify_task(task, headline: str, body: str = "") -> bool:
    """Convenience wrapper — builds a "headline\\n\\nbody\\n\\n<chat url>" message."""
    chat_url = f"{settings.CHAT_BASE_URL}/tasks/{task.pk}/"
    lines = [f"{headline} — task {task.pk}: {task.title}"]
    if body:
        lines.append("")
        lines.append(body.strip()[:800])
    lines.append("")
    lines.append(chat_url)
    return notify("\n".join(lines))


def notify(text: str) -> bool:
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "") or ""
    chat_id = getattr(settings, "TELEGRAM_CHAT_ID", "") or ""
    if not token or not chat_id or not text:
        return False
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text[:4000],  # Telegram caps at 4096
                "disable_web_page_preview": "true",
            },
            timeout=10.0,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram send error: %s", exc)
        return False
    if r.status_code != 200:
        log.warning("telegram %s: %s", r.status_code, r.text[:200])
        return False
    return True
