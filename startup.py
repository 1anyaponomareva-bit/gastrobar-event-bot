"""
Локальный и production startup: .env, lock, scheduler, polling, graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError

from bot_instance_lock import release_bot_lock, try_acquire_bot_lock
from config import (
    ADMIN_ID,
    DATABASE_PATH,
    GEMINI_API_KEY,
    RUN_MODE,
    TELEGRAM_BOT_TOKEN,
    TIMEZONE,
    is_local_run,
    is_railway_run,
)
from database import init_db
from fatal_conflict_dispatcher import FatalConflictDispatcher
from gemini_client import effective_gemini_model
from scheduler import setup_jobs, shutdown_scheduler, start_scheduler

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent
_LOCK_PATH = _PROJECT_ROOT / "bot.lock"


def log_startup_banner() -> None:
    if is_local_run():
        log.info("=" * 60)
        log.info("LOCAL MODE ACTIVE")
        log.info("=" * 60)
    log.info("RUN_MODE=%s", RUN_MODE)
    log.info("Project root: %s", _PROJECT_ROOT)
    log.info("Database: %s", DATABASE_PATH)
    log.info("Timezone: %s", TIMEZONE)
    log.info("Token loaded: %s", "yes" if TELEGRAM_BOT_TOKEN else "no")
    log.info("GEMINI_API_KEY loaded: %s", "yes" if GEMINI_API_KEY else "no")
    log.info("GEMINI_MODEL: %s", effective_gemini_model())
    if is_railway_run():
        import os

        log.info(
            "Railway (optional): RAILWAY_ENVIRONMENT=%r RAILWAY_SERVICE_NAME=%r",
            os.getenv("RAILWAY_ENVIRONMENT"),
            os.getenv("RAILWAY_SERVICE_NAME"),
        )
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY пуст — /events и radar не будут работать")
    if not ADMIN_ID:
        log.warning("ADMIN_ID не задан — планировщик weekly/daily отключён")


def acquire_instance_lock() -> bool:
    """Локально — bot.lock; на Railway lock не нужен (один реплика)."""
    if is_railway_run():
        log.info("RUN_MODE=railway — bot.lock skipped (single replica)")
        return True
    if try_acquire_bot_lock(_LOCK_PATH, log):
        log.info("bot.lock acquired: %s", _LOCK_PATH)
        return True
    return False


def release_instance_lock() -> None:
    if not is_railway_run():
        release_bot_lock(_LOCK_PATH, log)


def validate_required_config() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is missing. Add it to .env (see .env.example)."
        )


def setup_scheduler_if_admin(bot: Bot) -> None:
    if ADMIN_ID:
        setup_jobs(bot)
        start_scheduler()
        log.info("Scheduler started (ADMIN_ID=%s)", ADMIN_ID)
    else:
        log.warning("ADMIN_ID not set — scheduler disabled (/events and /daily still work)")


def install_graceful_shutdown(
    dp: Dispatcher,
    bot: Bot,
    *,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """SIGINT/SIGTERM → остановка polling и scheduler (Windows + Unix)."""
    loop = loop or asyncio.get_running_loop()
    shutting_down = False

    async def _shutdown(sig: signal.Signals | None = None) -> None:
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        name = sig.name if sig else "shutdown"
        log.info("Graceful shutdown (%s)...", name)
        try:
            await dp.stop_polling()
        except Exception:
            log.debug("stop_polling", exc_info=True)
        shutdown_scheduler()
        try:
            await bot.session.close()
        except Exception:
            log.debug("bot.session.close", exc_info=True)
        log.info("Shutdown complete")

    def _handler(sig: int, _frame: Any = None) -> None:
        try:
            signum = signal.Signals(sig)
        except ValueError:
            signum = None
        log.info("Signal received: %s", signum or sig)
        asyncio.create_task(_shutdown(signum))

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, lambda s=sig: _handler(s))
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler недоступен в главном потоке — KeyboardInterrupt в __main__
            signal.signal(sig, _handler)


async def run_polling(dp: Dispatcher, bot: Bot) -> None:
    """Polling с graceful shutdown hooks."""
    install_graceful_shutdown(dp, bot)
    log.info("Polling started (single instance, conflict = fail-fast)")
    try:
        await dp.start_polling(bot, handle_signals=False)
    except TelegramConflictError:
        raise
    finally:
        shutdown_scheduler()
