"""
Окно «ближайшие 24 часа» — только Asia/Ho_Chi_Minh, сравнение aware local_datetime.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from event_time import _parse_time_flexible

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def vn_now() -> datetime:
    return datetime.now(VN_TZ)


def next24_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    now_local = now if now is not None else vn_now()
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=VN_TZ)
    else:
        now_local = now_local.astimezone(VN_TZ)
    end_local = now_local + timedelta(hours=24)
    return now_local, end_local


def resolve_event_local_datetime_vn(event: dict[str, Any]) -> datetime | None:
    """Канонический старт в VN — через normalize_event_datetime (timestamp → UTC → VN)."""
    from event_datetime_norm import normalize_event_datetime

    sport = str(event.get("sport", "") or "").strip().lower()
    return normalize_event_datetime(event, sport=sport)


def is_in_next24_window(
    event: dict[str, Any],
    *,
    now: datetime | None = None,
    log_checks: bool = True,
) -> bool:
    now_local, end_local = next24_bounds(now)
    event_local_dt = resolve_event_local_datetime_vn(event)
    title = str(event.get("title", "")).strip()

    if event_local_dt is None:
        if log_checks:
            log.info(
                "NEXT24 CHECK: title=%r local=None include=False (no local datetime)",
                title,
            )
        return False

    include = now_local <= event_local_dt <= end_local
    if log_checks:
        log.info(
            "NEXT24 CHECK: title=%r local=%s include=%s",
            title,
            event_local_dt.isoformat(),
            include,
        )
    return include


def log_next24_window_header(now: datetime | None = None) -> tuple[datetime, datetime]:
    now_local, end_local = next24_bounds(now)
    log.info("NEXT24 NOW LOCAL: %s", now_local.isoformat())
    log.info("NEXT24 END LOCAL: %s", end_local.isoformat())
    return now_local, end_local
