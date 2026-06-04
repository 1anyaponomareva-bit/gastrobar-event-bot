"""
Лимиты API-SPORTS (free ~100 запросов/день): счётчик + пауза между запросами.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from config import DATABASE_PATH

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_lock = threading.Lock()
_async_lock = asyncio.Lock()
_last_call_monotonic: float = 0.0

# Ниже 100/день с запасом (dashboard Free)
SPORTS_API_DAILY_MAX = int(
    __import__("os").getenv("SPORTS_API_DAILY_MAX", "85") or "85"
)
SPORTS_API_PER_GROUP_MAX = int(
    __import__("os").getenv("SPORTS_API_PER_GROUP_MAX", "85") or "85"
)
SPORTS_API_MIN_INTERVAL_SEC = float(
    __import__("os").getenv("SPORTS_API_MIN_INTERVAL_SEC", "2.5") or "2.5"
)
SPORTS_API_WEEKLY_MAX_CALLS = int(
    __import__("os").getenv("SPORTS_API_WEEKLY_MAX_CALLS", "14") or "14"
)

_session_calls = 0


class SportsApiQuotaExceeded(Exception):
    """Локальный стоп — не бить API-SPORTS сверх лимита."""


def _vn_today() -> str:
    return datetime.now(VN_TZ).date().isoformat()


def api_group_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    m = re.search(
        r"v\d+\.(football|basketball|hockey|formula-1|esports|mma|nba|"
        r"baseball|afl|rugby|volleyball|american-football|handball)\.api-sports\.io",
        host,
    )
    return m.group(1) if m else "other"


def _get_counts_sync(day_vn: str) -> tuple[int, dict[str, int]]:
    with sqlite3.connect(DATABASE_PATH) as conn:
        row = conn.execute(
            "SELECT total_calls FROM sports_api_daily_usage WHERE day_vn = ?",
            (day_vn,),
        ).fetchone()
        total = int(row[0]) if row else 0
        groups = conn.execute(
            "SELECT api_group, call_count FROM sports_api_group_usage WHERE day_vn = ?",
            (day_vn,),
        ).fetchall()
    return total, {str(g): int(c) for g, c in groups}


def get_sports_api_usage_sync() -> dict[str, int | str]:
    day = _vn_today()
    total, groups = _get_counts_sync(day)
    return {
        "day_vn": day,
        "total": total,
        "total_max": SPORTS_API_DAILY_MAX,
        "remaining": max(0, SPORTS_API_DAILY_MAX - total),
        "groups": groups,
        "session_calls": _session_calls,
        "session_max": SPORTS_API_WEEKLY_MAX_CALLS,
    }


def reset_weekly_session_budget() -> None:
    global _session_calls
    _session_calls = 0


def _check_limits_sync(url: str) -> None:
    day = _vn_today()
    group = api_group_from_url(url)
    total, groups = _get_counts_sync(day)
    g_used = groups.get(group, 0)

    if total >= SPORTS_API_DAILY_MAX:
        raise SportsApiQuotaExceeded(
            f"daily_total {total}/{SPORTS_API_DAILY_MAX}"
        )
    if g_used >= SPORTS_API_PER_GROUP_MAX:
        raise SportsApiQuotaExceeded(
            f"daily_{group} {g_used}/{SPORTS_API_PER_GROUP_MAX}"
        )
    if _session_calls >= SPORTS_API_WEEKLY_MAX_CALLS:
        raise SportsApiQuotaExceeded(
            f"session {_session_calls}/{SPORTS_API_WEEKLY_MAX_CALLS}"
        )


def _record_success_sync(url: str) -> int:
    global _session_calls, _last_call_monotonic
    day = _vn_today()
    group = api_group_from_url(url)
    now_iso = datetime.now(VN_TZ).isoformat()
    with _lock:
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute(
                "SELECT total_calls FROM sports_api_daily_usage WHERE day_vn = ?",
                (day,),
            ).fetchone()
            total = int(row[0]) + 1 if row else 1
            conn.execute(
                """
                INSERT INTO sports_api_daily_usage (day_vn, total_calls, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(day_vn) DO UPDATE SET
                    total_calls = excluded.total_calls,
                    updated_at = excluded.updated_at
                """,
                (day, total, now_iso),
            )
            row_g = conn.execute(
                """
                SELECT call_count FROM sports_api_group_usage
                WHERE day_vn = ? AND api_group = ?
                """,
                (day, group),
            ).fetchone()
            g_count = int(row_g[0]) + 1 if row_g else 1
            conn.execute(
                """
                INSERT INTO sports_api_group_usage (day_vn, api_group, call_count, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day_vn, api_group) DO UPDATE SET
                    call_count = excluded.call_count,
                    updated_at = excluded.updated_at
                """,
                (day, group, g_count, now_iso),
            )
            conn.commit()
        _session_calls += 1
        _last_call_monotonic = time.monotonic()
    log.info(
        "API-SPORTS call recorded: group=%s day_total=%s session=%s url=%s",
        group,
        total,
        _session_calls,
        url[:80],
    )
    return total


async def _wait_rpm_slot_async() -> None:
    global _last_call_monotonic
    async with _async_lock:
        if _last_call_monotonic > 0:
            elapsed = time.monotonic() - _last_call_monotonic
            if elapsed < SPORTS_API_MIN_INTERVAL_SEC:
                await asyncio.sleep(SPORTS_API_MIN_INTERVAL_SEC - elapsed)


async def acquire_api_call(url: str) -> None:
    """Пауза RPM + проверка дневного/сессионного лимита. Исключение = не вызывать API."""
    await asyncio.to_thread(_check_limits_sync, url)
    await _wait_rpm_slot_async()


async def record_api_success(url: str) -> None:
    await asyncio.to_thread(_record_success_sync, url)


def ensure_db_tables_sync() -> None:
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sports_api_daily_usage (
                day_vn TEXT PRIMARY KEY,
                total_calls INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sports_api_group_usage (
                day_vn TEXT NOT NULL,
                api_group TEXT NOT NULL,
                call_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (day_vn, api_group)
            )
            """
        )
        conn.commit()
