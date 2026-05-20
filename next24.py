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
    """
    Canonical старт события в VN: local_datetime (aware) или local_date + local_time.
    Naive ISO без зоны трактуется как Asia/Ho_Chi_Minh (не UTC).
    """
    raw = str(event.get("local_datetime", "")).strip()
    if raw:
        s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=VN_TZ)
            return dt.astimezone(VN_TZ)

    date_s = str(event.get("local_date") or event.get("date", "")).strip()
    time_s = str(
        event.get("local_time")
        or event.get("time")
        or event.get("display_time")
        or event.get("time_display")
        or ""
    ).strip()
    time_s = time_s.removeprefix("≈").strip()
    if time_s == "время уточняется" or not time_s:
        return None
    if not _DATE_RE.match(date_s):
        return None
    norm, _ = _parse_time_flexible(time_s)
    if not norm:
        return None
    try:
        d = date.fromisoformat(date_s)
        hh, mm = map(int, norm.split(":"))
        return datetime.combine(d, dtime(hh, mm), tzinfo=VN_TZ)
    except ValueError:
        return None


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
