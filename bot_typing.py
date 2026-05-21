"""Индикатор «печатает…» в Telegram на время долгих операций."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import Message

log = logging.getLogger(__name__)

_TYPING_REFRESH_SEC = 4.0


@asynccontextmanager
async def show_typing(
    bot: Bot,
    chat_id: int,
    *,
    interval_sec: float = _TYPING_REFRESH_SEC,
) -> AsyncIterator[None]:
    """ChatAction.TYPING внизу чата, обновление каждые ~4 с."""

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


async def _edit_status_safe(msg: Message, text: str) -> None:
    try:
        await msg.edit_text(text)
    except Exception:
        log.debug("status edit failed", exc_info=True)


async def radar_search_progress(status_msg: Message, label: str) -> None:
    """Обновляет статусное сообщение (Event Radar ≤30 с, без Gemini Search)."""
    steps = [
        (8, f"🔍 Ищу: {label}\n✍️ BetBoom линия…"),
        (18, f"🔍 Фильтр времени (VN)…\n✍️ ~30 сек"),
        (26, f"🔍 Почти готово…\n✍️ подождите"),
    ]
    for delay, text in steps:
        await asyncio.sleep(delay)
        await _edit_status_safe(status_msg, text)


@asynccontextmanager
async def long_operation_typing(
    bot: Bot,
    chat_id: int,
    *,
    initial_text: str,
    progress_label: str = "",
) -> AsyncIterator[Message]:
    """
    «Печатает» + стартовое сообщение + периодические обновления статуса.
    Возвращает Message статуса (можно удалить после успеха).
    """
    status_msg = await bot.send_message(chat_id, initial_text)
    progress_task: asyncio.Task | None = None
    if progress_label:
        progress_task = asyncio.create_task(radar_search_progress(status_msg, progress_label))

    async with show_typing(bot, chat_id):
        try:
            yield status_msg
        finally:
            if progress_task:
                progress_task.cancel()
                with suppress(asyncio.CancelledError):
                    await progress_task
