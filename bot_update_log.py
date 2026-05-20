"""Логирование входящих апдейтов Telegram (диагностика «бот молчит»)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

log = logging.getLogger(__name__)


class LogUpdatesMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            uid = event.update_id
            parts: list[str] = [f"update_id={uid}"]
            if event.message:
                m = event.message
                parts.append(f"message chat={m.chat.id} type={m.chat.type}")
                parts.append(f"text={m.text!r}"[:120])
            if event.callback_query:
                parts.append(f"callback={event.callback_query.data!r}")
            log.info("TG %s", " ".join(parts))
        elif isinstance(event, Message):
            log.info(
                "TG message chat=%s type=%s text=%r",
                event.chat.id,
                event.chat.type,
                (event.text or "")[:80],
            )
        elif isinstance(event, CallbackQuery):
            log.info("TG callback data=%r", event.data)
        return await handler(event, data)
