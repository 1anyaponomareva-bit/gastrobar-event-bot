"""
Учёт вызовов Gemini (free tier: ~5 RPM, ~20 RPD) + rate-limit guard.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from config import DATABASE_PATH

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_lock = threading.Lock()
_last_call_monotonic: float = 0.0

# Запас ниже лимита 20 RPD / 5 RPM на free tier
GEMINI_FREE_RPD_SAFE = int(os.getenv("GEMINI_FREE_RPD_SAFE", "17") or "17")
GEMINI_FREE_RPM_MIN_INTERVAL = float(
    os.getenv("GEMINI_FREE_RPM_MIN_INTERVAL", "13") or "13"
)


def _vn_today_str() -> str:
    return datetime.now(VN_TZ).date().isoformat()


def _get_count_sync(day_vn: str) -> int:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT call_count FROM gemini_daily_usage WHERE day_vn = ?",
            (day_vn,),
        ).fetchone()
    return int(row[0]) if row else 0


def get_gemini_calls_today_sync() -> int:
    return _get_count_sync(_vn_today_str())


def record_gemini_call_sync(purpose: str) -> int:
    """После успешного generateContent. Возвращает count за сегодня (VN)."""
    global _last_call_monotonic
    day_vn = _vn_today_str()
    now_iso = datetime.now(VN_TZ).isoformat()
    with _lock:
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute(
                "SELECT call_count FROM gemini_daily_usage WHERE day_vn = ?",
                (day_vn,),
            ).fetchone()
            count = int(row[0]) + 1 if row else 1
            conn.execute(
                """
                INSERT INTO gemini_daily_usage (day_vn, call_count, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(day_vn) DO UPDATE SET
                    call_count = excluded.call_count,
                    updated_at = excluded.updated_at
                """,
                (day_vn, count, now_iso),
            )
            conn.commit()
        _last_call_monotonic = time.monotonic()
    log.info("GEMINI CALL USED: purpose=%s count_today=%s", purpose, count)
    return count


def wait_gemini_rpm_slot_sync() -> None:
    """~5 RPM → пауза между вызовами."""
    global _last_call_monotonic
    with _lock:
        if _last_call_monotonic <= 0:
            return
        elapsed = time.monotonic() - _last_call_monotonic
        if elapsed < GEMINI_FREE_RPM_MIN_INTERVAL:
            delay = GEMINI_FREE_RPM_MIN_INTERVAL - elapsed
            log.info("GEMINI RPM guard: sleep %.1fs", delay)
            time.sleep(delay)


def should_skip_gemini_discovery_sync() -> bool:
    return get_gemini_calls_today_sync() >= GEMINI_FREE_RPD_SAFE


def gemini_quota_remaining_sync() -> int:
    return max(0, GEMINI_FREE_RPD_SAFE - get_gemini_calls_today_sync())


def can_make_gemini_call_sync(*, purpose: str) -> tuple[bool, str]:
    if should_skip_gemini_discovery_sync():
        return False, "rpd_near_limit"
    wait_gemini_rpm_slot_sync()
    return True, ""


async def get_gemini_calls_today() -> int:
    import asyncio

    return await asyncio.to_thread(get_gemini_calls_today_sync)


async def should_skip_gemini_discovery() -> bool:
    import asyncio

    return await asyncio.to_thread(should_skip_gemini_discovery_sync)
