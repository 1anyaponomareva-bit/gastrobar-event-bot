"""Автоматическая рассылка weekly Event Radar админу."""

from __future__ import annotations

import logging

from aiogram import Bot

from config import ADMIN_ID
from event_radar import format_radar_week_message, get_event_radar_week
from runtime_messages import troubleshoot_footer

log = logging.getLogger(__name__)


async def run_scheduled_weekly_radar(bot: Bot) -> None:
    log.info("scheduled weekly radar started")
    if not ADMIN_ID:
        log.warning("scheduled weekly radar: ADMIN_ID not set")
        return

    try:
        events, raw_total, pre_count, selected, fetch_note = await get_event_radar_week()
        log.info(
            "scheduled weekly radar: raw=%s pre=%s selected=%s note=%s",
            raw_total,
            pre_count,
            selected,
            fetch_note,
        )
        if not events:
            await bot.send_message(
                ADMIN_ID,
                "📭 На ближайшие 3 дня в Gastrobar не нашлось крупных эфиров.\n"
                "Попробуйте /events → Афиша на 3 дня позже."
            )
            return

        body = format_radar_week_message(events)
        header = "🔭 Event Radar · Авто-афиша на 3 дня\n\n"
        if fetch_note:
            from event_radar import radar_fetch_header

            note = radar_fetch_header(fetch_note)
            if note:
                header = f"{note}\n\n{header}"

        await bot.send_message(
            ADMIN_ID,
            f"{header}Найдено {selected} событий (watchability).\n\n{body}",
        )
        log.info("scheduled weekly radar delivered: %s events", len(events))
    except Exception as exc:
        log.exception("Scheduler weekly_radar failed", exc_info=True)
        try:
            from error_handling import format_telegram_exception

            await bot.send_message(
                ADMIN_ID,
                f"❌ Не удалось собрать weekly Event Radar.\n\n"
                f"{format_telegram_exception(exc)}\n\n{troubleshoot_footer()}",
            )
        except Exception:
            log.exception("scheduled weekly radar: failed to notify admin", exc_info=True)
