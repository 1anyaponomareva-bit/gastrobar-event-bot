"""Одно Telegram-уведомление админу при конфликте getUpdates (не спам при деплое)."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent
_COOLDOWN_FILE = _PROJECT_ROOT / ".conflict_notify_at"
_DEFAULT_COOLDOWN_SEC = 30 * 60


def _cooldown_seconds() -> int:
    import os

    raw = os.getenv("CONFLICT_NOTIFY_COOLDOWN_SEC", "").strip()
    if not raw:
        return _DEFAULT_COOLDOWN_SEC
    try:
        return max(60, int(raw))
    except ValueError:
        return _DEFAULT_COOLDOWN_SEC


def should_send_conflict_telegram() -> bool:
    """Не чаще одного сообщения за CONFLICT_NOTIFY_COOLDOWN_SEC (все процессы на ПК)."""
    if not _COOLDOWN_FILE.is_file():
        return True
    try:
        data = json.loads(_COOLDOWN_FILE.read_text(encoding="utf-8"))
        last = float(data.get("ts", 0))
    except (OSError, ValueError, TypeError):
        return True
    return (time.time() - last) >= _cooldown_seconds()


def mark_conflict_telegram_sent() -> None:
    try:
        _COOLDOWN_FILE.write_text(
            json.dumps({"ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("conflict notify cooldown file not written: %s", e)


def send_conflict_telegram_once() -> bool:
    """Отправить предупреждение админу, если не было недавнего. Возвращает True если отправили."""
    if not should_send_conflict_telegram():
        log.info(
            "conflict notify skipped (cooldown %ss, file=%s)",
            _cooldown_seconds(),
            _COOLDOWN_FILE,
        )
        return False

    try:
        import json as json_mod
        import urllib.error
        import urllib.request

        from config import ADMIN_ID, TELEGRAM_BOT_TOKEN, is_railway_run

        if not (ADMIN_ID and TELEGRAM_BOT_TOKEN):
            return False
        if is_railway_run():
            return False

        mins = _cooldown_seconds() // 60
        text = (
            "⚠️ Локальный Gastrobar-бот остановлен (TelegramConflictError).\n\n"
            "Другой экземпляр уже держит getUpdates — обычно это **бот на Railway**.\n\n"
            "• Не запускайте `start_bot.bat`, пока работает облако.\n"
            "• Закройте все окна с `main.py` на этом ПК.\n\n"
            f"Повторные уведомления не чаще 1 раза в {mins} мин."
        )
        payload = json_mod.dumps(
            {"chat_id": ADMIN_ID, "text": text},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        mark_conflict_telegram_sent()
        return True
    except (urllib.error.URLError, OSError, ValueError) as e:
        log.warning("conflict notify send failed: %s", e)
        return False
