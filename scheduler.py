"""Планировщик: ежедневный готовый пост в 11:00 Asia/Ho_Chi_Minh."""

from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from zoneinfo import ZoneInfo

from config import (
    ADMIN_ID,
    DAILY_POST_HOUR,
    TIMEZONE,
    WEEKLY_RADAR_DOW,
    WEEKLY_RADAR_HOUR,
    WEEKLY_RADAR_MINUTE,
)
from daily_event_posts import run_scheduled_daily_content
from weekly_radar_posts import run_scheduled_weekly_radar

log = logging.getLogger(__name__)

TZ = ZoneInfo(TIMEZONE)
scheduler = AsyncIOScheduler(timezone=TZ)


def setup_jobs(bot: Bot) -> None:
    scheduler.add_job(
        run_scheduled_daily_content,
        CronTrigger(hour=DAILY_POST_HOUR, minute=0, timezone=TZ),
        args=[bot],
        id="daily_content_generator",
        replace_existing=True,
    )
    log.info(
        "Scheduled daily content at %02d:00 %s for ADMIN_ID=%s",
        DAILY_POST_HOUR,
        TIMEZONE,
        ADMIN_ID,
    )
    scheduler.add_job(
        run_scheduled_weekly_radar,
        CronTrigger(
            day_of_week=WEEKLY_RADAR_DOW,
            hour=WEEKLY_RADAR_HOUR,
            minute=WEEKLY_RADAR_MINUTE,
            timezone=TZ,
        ),
        args=[bot],
        id="weekly_radar_auto",
        replace_existing=True,
    )
    log.info(
        "Scheduled weekly radar: dow=%s %02d:%02d %s",
        WEEKLY_RADAR_DOW,
        WEEKLY_RADAR_HOUR,
        WEEKLY_RADAR_MINUTE,
        TIMEZONE,
    )


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        log.info("APScheduler запущен (%s)", TZ)


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("APScheduler остановлен")
