"""
Dispatcher, который не крутит бесконечный backoff при TelegramConflictError:
другой клиент уже держит getUpdates — выходим сразу (fail-fast, без бесконечного retry).
"""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from aiogram import loggers
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG, Dispatcher
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from aiogram.utils.backoff import Backoff, BackoffConfig

from config import is_local_run

_LOCAL_CONFLICT_RETRIES = 12
_LOCAL_CONFLICT_WAIT_SEC = 10.0

if TYPE_CHECKING:
    from aiogram.client.bot import Bot
    from aiogram.types import Update


def _notify_admin_conflict() -> None:
    try:
        import json
        import urllib.error
        import urllib.request

        from config import ADMIN_ID, TELEGRAM_BOT_TOKEN

        if not (ADMIN_ID and TELEGRAM_BOT_TOKEN):
            return
        text = (
            "⚠️ Локальный Gastrobar-бот остановлен (TelegramConflictError).\n\n"
            "Другой экземпляр уже держит getUpdates.\n"
            "Закройте все окна с ботом или Revoke token в BotFather.\n\n"
            "Пока конфликт есть, локальный бот не получает сообщения."
        )
        payload = json.dumps(
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
    except (urllib.error.URLError, OSError, ValueError):
        pass


def _exit_on_conflict(context: str) -> None:
    loggers.dispatcher.error(
        "%s: TelegramConflictError — другой клиент уже вызывает getUpdates "
        "(второй экземпляр main.py, webhook или другой deployment). "
        "Процесс завершается без retry.",
        context,
    )
    _notify_admin_conflict()
    print(
        "\n"
        "=" * 62 + "\n"
        "  TelegramConflictError — локальный бот НЕ работает\n"
        "=" * 62 + "\n"
        "Drugoj ekzemplyar uzhe oprashivaet Telegram.\n\n"
        "1. Zakroyte VSE okna start_bot.bat\n"
        "2. scripts\\list_bot_processes.ps1\n"
        "3. Esli Conflict: BotFather -> Revoke -> novyj token v .env\n"
        "4. start_bot.bat\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


class FatalConflictDispatcher(Dispatcher):
    """Как Dispatcher, но TelegramConflictError не глотается циклом retry."""

    @classmethod
    async def _listen_updates(
        cls,
        bot: Bot,
        polling_timeout: int = 30,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
    ) -> AsyncGenerator[Update, None]:
        backoff = Backoff(config=backoff_config)
        get_updates = GetUpdates(timeout=polling_timeout, allowed_updates=allowed_updates)
        kwargs: dict = {}
        if bot.session.timeout:
            kwargs["request_timeout"] = int(bot.session.timeout + polling_timeout)
        failed = False
        conflict_attempt = 0
        while True:
            try:
                updates = await bot(get_updates, **kwargs)
                conflict_attempt = 0
            except TelegramConflictError:
                if is_local_run() and conflict_attempt < _LOCAL_CONFLICT_RETRIES:
                    conflict_attempt += 1
                    loggers.dispatcher.warning(
                        "listen_updates: conflict %s/%s — ждём %.0fs (другой клиент отпустит getUpdates)",
                        conflict_attempt,
                        _LOCAL_CONFLICT_RETRIES,
                        _LOCAL_CONFLICT_WAIT_SEC,
                    )
                    import asyncio

                    await asyncio.sleep(_LOCAL_CONFLICT_WAIT_SEC)
                    continue
                _exit_on_conflict("listen_updates")
            except Exception as e:  # noqa: BLE001
                failed = True
                loggers.dispatcher.error("Failed to fetch updates - %s: %s", type(e).__name__, e)
                loggers.dispatcher.warning(
                    "Sleep for %f seconds and try again... (tryings = %d, bot id = %d)",
                    backoff.next_delay,
                    backoff.counter,
                    bot.id,
                )
                await backoff.asleep()
                continue

            if failed:
                loggers.dispatcher.info(
                    "Connection established (tryings = %d, bot id = %d)",
                    backoff.counter,
                    bot.id,
                )
                backoff.reset()
                failed = False

            for update in updates:
                yield update
                get_updates.offset = update.update_id + 1

    async def start_polling(self, *bots: Bot, **kwargs: Any) -> None:
        try:
            await super().start_polling(*bots, **kwargs)
        except TelegramConflictError:
            _exit_on_conflict("start_polling")
