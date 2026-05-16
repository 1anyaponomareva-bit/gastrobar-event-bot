"""Индикатор «печатает…» в Telegram на время долгих операций."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from aiogram import Bot
from aiogram.enums import ChatAction

log = logging.getLogger(__name__)

_TYPING_REFRESH_SEC = 4.0


@asynccontextmanager
async def show_typing(
    bot: Bot,
    chat_id: int,
    *,
    interval_sec: float = _TYPING_REFRESH_SEC,
) -> AsyncIterator[None]:
    """
    Показывает ChatAction.TYPING и обновляет каждые ~4 с (лимит Telegram).
  """

    async def _loop() -> None:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except Exception:
                log.debug("send_chat_action typing failed", exc_info=True)
            await asyncio.sleep(interval_sec)

    task = asyncio.create_task(_loop())
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        log.debug("initial typing action failed", exc_info=True)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
