"""
Dispatcher при TelegramConflictError: локально — короткий retry; на Railway — длинный retry
перед выходом (при деплое старый контейнер временно держит getUpdates).
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from aiogram import loggers
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramUnauthorizedError
from aiogram.methods import GetUpdates
from aiogram.utils.backoff import Backoff, BackoffConfig

from config import is_local_run, is_railway_run

# Локально — короткое ожидание (локальный второй процесс нужно явно закрыть).
_LOCAL_CONFLICT_RETRIES = 3
_LOCAL_CONFLICT_WAIT_SEC = 5.0

# Railway: пока деплоится новый контейнер, старый ещё держит getUpdates — ждём отпускания.
try:
    _RAILWAY_CONFLICT_RETRIES = max(12, int(os.getenv("RAILWAY_CONFLICT_RETRIES", "40") or "40"))
except ValueError:
    _RAILWAY_CONFLICT_RETRIES = 40
try:
    # Чуть дольше пауза — при rolling deploy старый контейнер иногда >25s отпускает polling
    _RAILWAY_CONFLICT_WAIT_SEC = float(os.getenv("RAILWAY_CONFLICT_WAIT_SEC", "6") or "6")
except ValueError:
    _RAILWAY_CONFLICT_WAIT_SEC = 6.0

if TYPE_CHECKING:
    from aiogram.client.bot import Bot
    from aiogram.types import Update


def _poll_conflict_retry_budget() -> tuple[int, float, str]:
    """Сколько раз ждать TelegramConflict перед жёстким выходом."""
    if is_railway_run():
        return (
            _RAILWAY_CONFLICT_RETRIES,
            max(2.0, _RAILWAY_CONFLICT_WAIT_SEC),
            "railway_deploy",
        )
    if is_local_run():
        return _LOCAL_CONFLICT_RETRIES, _LOCAL_CONFLICT_WAIT_SEC, "local"
    return _LOCAL_CONFLICT_RETRIES, _LOCAL_CONFLICT_WAIT_SEC, "other"


def _exit_on_conflict(context: str) -> None:
    loggers.dispatcher.error(
        "%s: TelegramConflictError после всех попыток — второй клиент getUpdates.",
        context,
    )
    from conflict_notify import send_conflict_telegram_once

    send_conflict_telegram_once()
    if is_railway_run():
        print(
            "\n"
            + "=" * 62
            + "\n"
            + "  TelegramConflictError on Railway — still another getUpdates client.\n"
            + "  Check: duplicate Railway service / local main.py.\n"
            + "=" * 62
            + "\n",
            file=sys.stderr,
        )
    else:
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
    os._exit(1)


def _exit_on_unauthorized(context: str) -> None:
    loggers.dispatcher.error(
        "%s: TelegramUnauthorizedError — неверный или отозванный TELEGRAM_BOT_TOKEN.",
        context,
    )
    if is_railway_run():
        print(
            "\n"
            + "=" * 62
            + "\n"
            + "  TelegramUnauthorizedError on Railway\n"
            + "  1. BotFather → ваш бот → API Token → скопируйте НОВЫЙ токен\n"
            + "  2. Railway → gastrobar-event-bot → Variables → TELEGRAM_BOT_TOKEN\n"
            + "  3. Вставьте без кавычек и пробелов → Save → Redeploy\n"
            + "  4. Дождитесь нового деплоя (старый контейнер после Revoke даёт Unauthorized)\n"
            + "=" * 62
            + "\n",
            file=sys.stderr,
        )
    else:
        print(
            "\n"
            + "=" * 62
            + "\n"
            + "  TELEGRAM_BOT_TOKEN недействителен (Revoke в BotFather?)\n"
            + "  Обновите .env и перезапустите start_bot.bat\n"
            + "=" * 62
            + "\n",
            file=sys.stderr,
        )
    os._exit(1)


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
                max_retries, wait_sec, bucket = _poll_conflict_retry_budget()
                if conflict_attempt < max_retries:
                    conflict_attempt += 1
                    hint = (
                        "пересечение деплоев / старый контейнер"
                        if bucket == "railway_deploy"
                        else "другой клиент должен закрыться"
                    )
                    if conflict_attempt == 1 and bucket == "railway_deploy":
                        loggers.dispatcher.warning(
                            "Railway: если конфликт не кончается за пару минут — часто "
                            "**два процесса с одним токеном** (Replicas>1 или второй сервис "
                            "с тем же TELEGRAM_BOT_TOKEN). Service → Settings: Replicas=1; "
                            "Pause лишний worker. Опционально: RAILWAY_PRE_POLL_DELAY_SEC=15"
                        )
                    loggers.dispatcher.warning(
                        "listen_updates: conflict %s/%s [%s] — ждём %.0fs (%s)",
                        conflict_attempt,
                        max_retries,
                        bucket,
                        wait_sec,
                        hint,
                    )
                    import asyncio

                    await asyncio.sleep(wait_sec)
                    continue
                _exit_on_conflict("listen_updates")
            except TelegramUnauthorizedError:
                _exit_on_unauthorized("listen_updates")
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
        except TelegramUnauthorizedError:
            _exit_on_unauthorized("start_polling")
