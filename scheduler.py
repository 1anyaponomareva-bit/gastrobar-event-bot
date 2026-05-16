"""Планировщик: еженедельная и ежедневная рассылка админу."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from config import ADMIN_ID
from database import replace_week_events
from keyboards import daily_alert_kb, week_actions_kb
from daily_event import fetch_week_events_for_daily, get_next_featured_event
from event_radar import (
    format_radar_scheduler_summary,
    get_event_radar_week,
    radar_events_to_db_rows,
)

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")

# События последнего дневного уведомления (для «Сделать пост»)
pending_daily_events: list[dict[str, Any]] | None = None

scheduler = AsyncIOScheduler(timezone=TZ)


def set_pending_daily(events: list[dict[str, Any]] | None) -> None:
    global pending_daily_events
    pending_daily_events = events


def get_pending_daily() -> list[dict[str, Any]] | None:
    return pending_daily_events


async def _send_weekly_afisha(bot: Bot) -> None:
    events, _, _, _, _ = await get_event_radar_week()
    await replace_week_events(radar_events_to_db_rows(events))
    text = format_radar_scheduler_summary(events)
    body = "Event Radar — неделя:\n\n" + text
    await bot.send_message(
        ADMIN_ID,
        body,
        reply_markup=week_actions_kb(),
    )


async def _send_daily_if_needed(bot: Bot) -> None:
    """Пост дня: одно featured-событие (~24 ч), не weekly radar."""
    pool = await fetch_week_events_for_daily()
    featured = get_next_featured_event(pool)
    if not featured:
        set_pending_daily(None)
        return
    set_pending_daily([featured])
    timing = str(featured.get("daily_timing_phrase", "")).strip()
    title = str(featured.get("title", "")).strip()
    display = str(
        featured.get("display_time")
        or featured.get("time_display")
        or featured.get("time", "")
    ).strip()
    msg = (
        "🔥 Пост дня — отдельная кампания (не недельная афиша)\n\n"
        f"{title}\n"
        f"Когда: {timing}"
        + (f" · {display}" if display else "")
    )
    await bot.send_message(
        ADMIN_ID,
        msg,
        reply_markup=daily_alert_kb(),
    )


def setup_jobs(bot: Bot) -> None:
    scheduler.add_job(
        _send_weekly_afisha,
        CronTrigger(day_of_week="mon", hour=11, minute=0, timezone=TZ),
        args=[bot],
        id="weekly_afisha",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_daily_if_needed,
        CronTrigger(hour=12, minute=0, timezone=TZ),
        args=[bot],
        id="daily_spotlight",
        replace_existing=True,
    )


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        log.info("APScheduler запущен (%s)", TZ)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("APScheduler остановлен")
