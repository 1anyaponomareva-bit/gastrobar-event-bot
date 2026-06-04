"""
Строка даты/времени для афиши Event Radar (Asia/Ho_Chi_Minh).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_WD = ("ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС")


def vn_now() -> datetime:
    return datetime.now(VN_TZ)


def _event_local_date(e: dict[str, Any]) -> date | None:
    from next24 import resolve_event_local_datetime_vn

    dt = resolve_event_local_datetime_vn(e)
    if dt is not None:
        return dt.astimezone(VN_TZ).date()
    ds = str(e.get("local_date") or e.get("date", "")).strip()
    if len(ds) >= 10:
        try:
            return date.fromisoformat(ds[:10])
        except ValueError:
            pass
    return None


def format_afisha_when(
    e: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    """
    Дата + день + время для строки афиши.

    Примеры:
      04.06, Сегодня 20:00
      05.06, Завтра 15:30
      06.06, СБ 20:00
    """
    now_local = now or vn_now()
    if now_local.tzinfo is None:
        now_local = now_local.replace(tzinfo=VN_TZ)
    else:
        now_local = now_local.astimezone(VN_TZ)

    tm = str(
        e.get("local_time") or e.get("display_time") or e.get("time", "")
    ).strip()
    event_d = _event_local_date(e)

    if event_d is None:
        wd = str(e.get("local_weekday") or e.get("weekday", "")).strip()
        return f"{wd} {tm}".strip() if wd else tm

    date_str = event_d.strftime("%d.%m")
    today = now_local.date()
    if event_d == today:
        day_lbl = "Сегодня"
    elif event_d == today + timedelta(days=1):
        day_lbl = "Завтра"
    else:
        day_lbl = _WD[event_d.weekday()]

    if tm:
        return f"{date_str}, {day_lbl} {tm}"
    return f"{date_str}, {day_lbl}"
