"""
Dispatcher, который не крутит бесконечный backoff при TelegramConflictError:
другой клиент уже держит getUpdates — выходим сразу.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from aiogram import loggers
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG, Dispatcher
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from aiogram.utils.backoff import Backoff, BackoffConfig

if TYPE_CHECKING:
    from aiogram.client.bot import Bot
    from aiogram.types import Update


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
        while True:
            try:
                updates = await bot(get_updates, **kwargs)
            except TelegramConflictError:
                loggers.dispatcher.error(
                    "TelegramConflictError: другой клиент вызывает getUpdates. Останавливаем polling."
                )
                raise
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
