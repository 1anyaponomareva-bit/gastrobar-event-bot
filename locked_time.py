"""
Immutable schedule time for Gastrobar events.
Canonical: utc_datetime only. One conversion to Asia/Ho_Chi_Minh.
Weekly and daily read the same locked fields — no recompute, no Gemini time.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from event_time import (
    TARGET_TZ,
    TARGET_TZ_NAME,
    _DATETIME_CANONICAL_KEYS,
    _DATE_RE,
    _TIME_RE,
    _event_blob,
    _parse_time_flexible,
    extract_source_fields_for_conversion,
    get_ru_weekday,
    is_valid_source_timezone,
    log_datetime_pipeline,
    parse_datetime_iso,
    source_to_utc_datetime,
    utc_datetime_to_local_fields,
)

log = logging.getLogger(__name__)

# Source TZ must not be Vietnam-local for international broadcasts.
_BOGUS_SOURCE_FOR_INTL = frozenset(
    {
        "Asia/Ho_Chi_Minh",
        "Asia/Bangkok",
        "ICT",
        "VIETNAM",
        "HO_CHI_MINH",
        "NHATRANG",
        "GMT+7",
        "UTC+7",
    }
)


def _is_international_broadcast(event: dict[str, Any]) -> bool:
    b = _event_blob(event)
    cat = str(event.get("category", "")).upper()
    if any(
        x in cat
        for x in (
            "FOOT",
            "SOCCER",
            "NBA",
            "NHL",
            "F1",
            "FORMULA",
            "UFC",
            "BOX",
            "ESPORT",
        )
    ):
        return True
    return bool(
        re.search(
            r"premier\s+league|champions\s+league|europa\s+league|"
            r"\bnba\b|\bnhl\b|formula\s*1|\bufc\b|grand\s+prix",
            b,
            re.I,
        )
    )


def is_acceptable_source_timezone(tz: str, event: dict[str, Any]) -> bool:
    t = str(tz or "").strip()
    if not t or not is_valid_source_timezone(t):
        return False
    norm = t.upper().replace(" ", "_")
    if _is_international_broadcast(event) and (
        norm in _BOGUS_SOURCE_FOR_INTL or t in _BOGUS_SOURCE_FOR_INTL
    ):
        log.error(
            "REJECTED bogus SOURCE TIMEZONE %r for intl event %r — "
            "likely Gemini/ICT confusion with UTC",
            tz,
            event.get("title"),
        )
        return False
    return True


def log_event_time_debug(event: dict[str, Any], *, phase: str = "") -> None:
    from timezone_truth import log_event_debug as _debug

    _debug(event, phase=phase)


def run_sanity_checks(event: dict[str, Any]) -> None:
    blob = _event_blob(event)
    tm = str(event.get("local_time") or event.get("time", "")).strip().removeprefix("≈")
    m = _TIME_RE.match(tm)
    if not m:
        return
    hour = int(m.group(1))
    title = event.get("title")

    is_epl = bool(re.search(r"premier\s+league|\bepl\b", blob, re.I)) or (
        "FOOT" in str(event.get("category", "")).upper()
        and "premier" in blob
    )
    is_top_foot = is_epl or bool(
        re.search(
            r"champions\s+league|europa\s+league|la\s+liga|serie\s+a|bundesliga",
            blob,
            re.I,
        )
    )
    if is_top_foot and hour in (12, 19):
        log.warning(
            "POTENTIALLY INVALID EPL TIME: %r at %s %s VN (hour=%s). "
            "EPL usually late evening / night / early morning VN. utc=%s source=%s %s %s",
            title,
            event.get("local_weekday"),
            tm,
            hour,
            event.get("utc_datetime"),
            event.get("original_timezone"),
            event.get("original_date"),
            event.get("original_time"),
        )
    elif is_top_foot and 9 <= hour <= 14:
        log.warning(
            "POTENTIALLY INVALID FOOTBALL TIME: %r at %s %s VN. utc=%s",
            title,
            event.get("local_weekday"),
            tm,
            event.get("utc_datetime"),
        )

    if re.search(r"\bnba\b", blob, re.I) and re.search(
        r"playoff|conference\s+final|finals", blob, re.I
    ):
        if 17 <= hour <= 23:
            log.warning(
                "POTENTIALLY INVALID NBA TIME: %r at %s %s VN — "
                "playoffs often late night / early morning VN. utc=%s",
                title,
                event.get("local_weekday"),
                tm,
                event.get("utc_datetime"),
            )


def has_locked_schedule(event: dict[str, Any]) -> bool:
    return bool(
        event.get("time_locked")
        and str(event.get("utc_datetime", "")).strip()
        and str(event.get("local_datetime", "")).strip()
    )


def lock_event_schedule(event: dict[str, Any], *, phase: str = "lock") -> dict[str, Any] | None:
    """Immutable schedule via timezone_truth (single UTC → VN conversion)."""
    from timezone_truth import establish_schedule

    out = establish_schedule(event, phase=phase)
    if out is None:
        return None
    log_datetime_pipeline(out)
    run_sanity_checks(out)
    return out


def reapply_local_from_utc(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Единственный переход UTC → VN по полю utc_datetime (без establish_schedule).
    Используется при загрузке weekly cache и для API-SPORTS.
    """
    utc_raw = str(event.get("utc_datetime", "")).strip()
    if not utc_raw:
        return None
    utc_dt = parse_datetime_iso(utc_raw)
    if utc_dt is None:
        return None
    out = dict(event)
    out.update(utc_datetime_to_local_fields(utc_dt))
    out["time_locked"] = True
    out["schedule_locked"] = True
    tm = str(out.get("local_time", ""))
    out["time_display"] = tm
    out["display_time"] = tm
    return out


def lock_event_from_api_utc_iso(
    event: dict[str, Any],
    dt_iso: str,
    *,
    phase: str = "api_sports",
) -> dict[str, Any] | None:
    """
    API-SPORTS fixture.date — authoritative UTC (Z). Один переход UTC → VN.
    Не вызывать establish_schedule: иначе Europe/London перетолкует 18:30Z как BST wall.
    """
    dt = parse_datetime_iso(dt_iso)
    if dt is None:
        return None
    utc_aware = dt.astimezone(timezone.utc)
    out = dict(event)
    out.update(utc_datetime_to_local_fields(utc_aware))
    out["original_timezone"] = "UTC"
    out["original_date"] = utc_aware.date().isoformat()
    out["original_time"] = utc_aware.strftime("%H:%M")
    out["source_timezone"] = "UTC"
    out["utc_authority"] = "api_sports"
    out["time_precision"] = "exact"
    out["verified_via"] = out.get("verified_via") or "API-SPORTS"
    tm = str(out.get("local_time", ""))
    out["time_display"] = tm
    out["display_time"] = tm
    out["time_locked"] = True
    out["schedule_locked"] = True

    from timezone_truth import log_event_debug

    log_event_debug(out, phase=phase)
    log_datetime_pipeline(out)
    run_sanity_checks(out)
    return out


def schedule_dict_for_formatters(event: dict[str, Any]) -> dict[str, str]:
    """Fields formatters may read — all derived from locked local time."""
    return {
        "utc_datetime": str(event.get("utc_datetime", "")),
        "local_datetime": str(event.get("local_datetime", "")),
        "weekday": str(event.get("local_weekday") or event.get("weekday", "")),
        "local_weekday": str(event.get("local_weekday") or event.get("weekday", "")),
        "time": str(event.get("local_time") or event.get("time", "")),
        "local_time": str(event.get("local_time") or event.get("time", "")),
        "display_time": str(event.get("local_time") or event.get("time", "")),
        "date": str(event.get("local_date") or event.get("date", "")),
        "local_date": str(event.get("local_date") or event.get("date", "")),
        "timezone": TARGET_TZ_NAME,
    }


def assert_no_time_drift(
    cached: dict[str, Any],
    current: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    """If times differ — CRITICAL log, return cached schedule."""
    if not has_locked_schedule(cached):
        return current
    c_utc = str(cached.get("utc_datetime", "")).strip()
    n_utc = str(current.get("utc_datetime", "")).strip()
    c_loc = str(cached.get("local_time", "")).strip()
    n_loc = str(current.get("local_time") or current.get("display_time", "")).strip()

    drift = False
    if n_utc and c_utc and n_utc != c_utc:
        drift = True
    elif n_loc and c_loc and n_loc != c_loc:
        drift = True

    if drift:
        log.critical(
            "CRITICAL TIME DRIFT [%s]: title=%r weekly_utc=%s weekly_local=%s "
            "recomputed_utc=%s recomputed_local=%s — using weekly cached",
            context,
            cached.get("title"),
            c_utc,
            c_loc,
            n_utc,
            n_loc,
        )
        merged = dict(current)
        for key in _DATETIME_CANONICAL_KEYS:
            if cached.get(key) is not None:
                merged[key] = cached[key]
        merged["time_locked"] = True
        merged["schedule_locked"] = True
        return merged
    return current


def get_event_start_vn(event: dict[str, Any]) -> datetime | None:
    from next24 import resolve_event_local_datetime_vn

    return resolve_event_local_datetime_vn(event)
