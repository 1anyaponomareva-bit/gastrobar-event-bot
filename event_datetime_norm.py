"""
Единая нормализация времени событий: API-SPORTS → aware UTC → Asia/Ho_Chi_Minh.

Используется WEEK, NOW24, daily, resolve_event_local_datetime_vn.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from event_time import parse_datetime_iso, utc_datetime_to_local_fields

log = logging.getLogger(__name__)

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")


def utc_from_timestamp(ts: Any) -> datetime | None:
    """Unix timestamp (сек или мс) → aware UTC."""
    if ts is None or ts == "":
        return None
    try:
        val = float(ts)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    if val > 1_000_000_000_000:
        val = val / 1000.0
    return datetime.fromtimestamp(val, tz=timezone.utc)


def utc_from_iso(iso: str) -> datetime | None:
    dt = parse_datetime_iso(str(iso or "").strip())
    if dt is None:
        return None
    return dt.astimezone(timezone.utc)


def extract_api_datetime_raw(raw: dict[str, Any]) -> dict[str, Any]:
    """
    Поля API-SPORTS для логов (football fixture.* или flat hockey/basketball).
    """
    fixture = raw.get("fixture") if isinstance(raw.get("fixture"), dict) else {}
    if not fixture:
        fixture = raw

    ts = (
        raw.get("fixture_timestamp")
        or raw.get("timestamp")
        or fixture.get("timestamp")
    )
    dt_iso = (
        str(raw.get("fixture_utc_iso") or "").strip()
        or str(fixture.get("date") or "").strip()
        or str(raw.get("date") or "").strip()
    )
    if dt_iso and "T" not in dt_iso and len(dt_iso) <= 10:
        time_part = str(raw.get("time") or fixture.get("time") or "").strip()
        if time_part:
            dt_iso = f"{dt_iso[:10]}T{time_part}"

    tz_name = str(
        raw.get("api_timezone")
        or raw.get("timezone")
        or fixture.get("timezone")
        or ""
    ).strip()

    api_date = ""
    api_time = ""
    if dt_iso and "T" in dt_iso:
        try:
            p = datetime.fromisoformat(
                dt_iso.replace("Z", "+00:00") if dt_iso.endswith("Z") else dt_iso
            )
            api_date = p.date().isoformat()
            api_time = p.strftime("%H:%M")
        except ValueError:
            api_date = dt_iso[:10]
    elif dt_iso:
        api_date = dt_iso[:10]

    return {
        "api_date": api_date,
        "api_time": api_time,
        "api_timestamp": ts,
        "api_timezone": tz_name,
        "fixture_utc_iso": dt_iso,
    }


def normalize_event_datetime(
    raw_event: dict[str, Any],
    sport: str = "",
) -> datetime | None:
    """
    Канонический старт в Asia/Ho_Chi_Minh (aware).

    1. fixture_timestamp / timestamp
    2. fixture_utc_iso / utc_datetime (ISO UTC)
    3. local_datetime (aware)
    4. local_date + local_time / date + time (уже VN wall)
    """
    e = raw_event

    utc_dt: datetime | None = None

    ts = e.get("fixture_timestamp") or e.get("api_timestamp")
    if ts is not None and ts != "":
        utc_dt = utc_from_timestamp(ts)

    if utc_dt is None:
        iso = (
            str(e.get("fixture_utc_iso") or "").strip()
            or str(e.get("utc_datetime") or "").strip()
        )
        if iso:
            utc_dt = utc_from_iso(iso)

    if utc_dt is None:
        loc_raw = str(e.get("local_datetime", "")).strip()
        if loc_raw:
            s = loc_raw[:-1] + "+00:00" if loc_raw.endswith("Z") else loc_raw
            try:
                loc = datetime.fromisoformat(s)
                if loc.tzinfo is None:
                    loc = loc.replace(tzinfo=VN_TZ)
                return loc.astimezone(VN_TZ)
            except ValueError:
                pass

    if utc_dt is not None:
        return utc_dt.astimezone(VN_TZ)

    date_s = str(e.get("local_date") or e.get("date", "")).strip()
    time_s = str(
        e.get("local_time")
        or e.get("time")
        or e.get("display_time")
        or ""
    ).strip().removeprefix("≈").strip()
    if not date_s or not time_s or time_s == "время уточняется":
        return None
    try:
        d = date.fromisoformat(date_s[:10])
    except ValueError:
        return None
    from event_time import _parse_time_flexible

    norm, _ = _parse_time_flexible(time_s)
    if not norm:
        return None
    try:
        hh, mm = map(int, norm.split(":"))
        return datetime.combine(d, dtime(hh, mm), tzinfo=VN_TZ)
    except ValueError:
        return None


def apply_normalized_datetime_fields(
    event: dict[str, Any],
    local_dt: datetime,
    *,
    utc_authority: str = "api_sports",
) -> dict[str, Any]:
    """Записать immutable datetime-поля на событие."""
    out = dict(event)
    utc_aware = local_dt.astimezone(timezone.utc)
    out.update(utc_datetime_to_local_fields(utc_aware))
    out["utc_authority"] = utc_authority
    out["time_locked"] = True
    out["schedule_locked"] = True
    tm = str(out.get("local_time", ""))
    out["time_display"] = tm
    out["display_time"] = tm
    return out


def _event_home_away(event: dict[str, Any]) -> tuple[str, str]:
    home = str(event.get("home") or "").strip()
    away = str(event.get("away") or "").strip()
    if home or away:
        return home, away
    title = str(event.get("title") or "")
    for sep in (" vs ", " — ", " - ", " – "):
        if sep in title:
            a, b = title.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""


def log_now24_event_raw(
    event: dict[str, Any],
    *,
    sport: str = "",
    now_local: datetime | None = None,
    end_local: datetime | None = None,
    phase: str = "raw",
) -> None:
    """Подробный лог одного события (обязателен для диагностики NOW24)."""
    from next24 import next24_bounds

    if now_local is None or end_local is None:
        now_local, end_local = next24_bounds()

    api = extract_api_datetime_raw(event)
    local_dt = normalize_event_datetime(event, sport=sport or str(event.get("sport", "")))
    utc_dt = local_dt.astimezone(timezone.utc) if local_dt else None
    home, away = _event_home_away(event)
    sp = sport or event.get("sport", "?")

    log.info(
        "NOW24_EVENT_RAW [%s]:\n"
        "sport=%s\n"
        "league=%s\n"
        "home=%s\n"
        "away=%s\n"
        "title=%s\n"
        "api_date=%s\n"
        "api_time=%s\n"
        "api_timestamp=%s\n"
        "api_timezone=%s\n"
        "parsed_utc=%s\n"
        "local_datetime=%s\n"
        "now_local=%s\n"
        "end_local=%s",
        phase,
        sp,
        (event.get("league") or "")[:80],
        home[:60],
        away[:60],
        (event.get("title") or "")[:100],
        api.get("api_date"),
        api.get("api_time"),
        api.get("api_timestamp"),
        api.get("api_timezone"),
        utc_dt.isoformat() if utc_dt else None,
        local_dt.isoformat() if local_dt else None,
        now_local.isoformat(),
        end_local.isoformat(),
    )


def log_now24_drop(
    event: dict[str, Any],
    reason: str,
    *,
    now_local: datetime | None = None,
    end_local: datetime | None = None,
) -> None:
    """Лог каждого отсечения по окну NOW24."""
    from next24 import next24_bounds

    if now_local is None or end_local is None:
        now_local, end_local = next24_bounds()

    local_dt = normalize_event_datetime(event, sport=str(event.get("sport", "")))
    delta_h = "?"
    if local_dt is not None:
        delta_h = round((local_dt - now_local).total_seconds() / 3600.0, 2)

    extra = ""
    if reason == "outside_window":
        extra = (
            f"\nnow_local={now_local.isoformat()}\nend_local={end_local.isoformat()}"
        )

    log.info(
        "NOW24_DROP:\n"
        "title=%s\n"
        "sport=%s\n"
        "league=%s\n"
        "local_datetime=%s\n"
        "reason=%s\n"
        "delta_hours=%s%s",
        (event.get("title") or "")[:100],
        event.get("sport", "?"),
        (event.get("league") or "")[:80],
        local_dt.isoformat() if local_dt else "?",
        reason,
        delta_h,
        extra,
    )
