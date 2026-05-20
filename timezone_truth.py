"""
Единый слой правды для времени событий.
Пайплайн: source wall-clock + trusted IANA → UTC → один раз Asia/Ho_Chi_Minh.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from event_time import (
    TARGET_TZ,
    TARGET_TZ_NAME,
    _DATE_RE,
    _TIME_RE,
    _event_blob,
    _parse_time_flexible,
    extract_source_fields,
    is_valid_source_timezone,
    parse_datetime_iso,
    resolve_zone,
    source_to_utc_datetime,
    utc_datetime_to_local_fields,
)
log = logging.getLogger(__name__)


def _is_international_broadcast(event: dict[str, Any]) -> bool:
    b = _event_blob(event)
    cat = str(event.get("category", "")).upper()
    if any(
        x in cat
        for x in ("FOOT", "SOCCER", "NBA", "NHL", "F1", "FORMULA", "UFC", "BOX", "ESPORT")
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
        norm in _BOGUS_SOURCE or t in _BOGUS_SOURCE
    ):
        return False
    return True

# Gemini / ICT — никогда как source для международного эфира
_BOGUS_SOURCE = frozenset(
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

# Приоритет: жёсткая карта лиг (перебивает Gemini)
_HARD_TZ_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"premier\s+league|\bepl\b", re.I), "Europe/London"),
    (re.compile(r"europa\s+league|\buel\b", re.I), "Europe/Paris"),
    (re.compile(r"champions\s+league|\bucl\b", re.I), "Europe/Paris"),
    (re.compile(r"la\s+liga|laliga", re.I), "Europe/Madrid"),
    (re.compile(r"serie\s+a", re.I), "Europe/Rome"),
    (re.compile(r"bundesliga", re.I), "Europe/Berlin"),
    (re.compile(r"ligue\s+1", re.I), "Europe/Paris"),
    (re.compile(r"eurovision", re.I), "Europe/Zurich"),
    (re.compile(r"formula\s*1|\bf1\b|grand\s+prix", re.I), "Europe/London"),
    (re.compile(r"canadian\s+gp|canada\s+gp|montreal", re.I), "America/Toronto"),
    (re.compile(r"\bnba\b", re.I), "America/New_York"),
    (re.compile(r"\bnhl\b|stanley\s+cup", re.I), "America/New_York"),
    (re.compile(r"\bufc\b", re.I), "America/New_York"),
]


def resolve_trusted_source_timezone(event: dict[str, Any]) -> str | None:
    """
    Официальная IANA-зона: hard map → валидный Gemini → эвристика event_time.
    """
    gemini_tz = str(
        event.get("source_timezone") or event.get("original_timezone") or ""
    ).strip()

    blob = _event_blob(event)
    for pat, iana in _HARD_TZ_RULES:
        if pat.search(blob):
            if gemini_tz and gemini_tz != iana and is_valid_source_timezone(gemini_tz):
                log.info(
                    "TRUSTED TZ OVERRIDE: %s -> %s for %r (gemini had %r)",
                    gemini_tz,
                    iana,
                    event.get("title"),
                    gemini_tz,
                )
            return iana

    if gemini_tz and is_acceptable_source_timezone(gemini_tz, event):
        norm = gemini_tz.upper().replace(" ", "_")
        if norm not in _BOGUS_SOURCE and gemini_tz not in _BOGUS_SOURCE:
            return gemini_tz

    from event_time import infer_source_timezone

    inferred = infer_source_timezone(event)
    if inferred and is_acceptable_source_timezone(inferred, event):
        return inferred
    return None


def extract_source_wall_clock(event: dict[str, Any]) -> tuple[str, str]:
    """Дата/время в зоне источника (не VN display fields)."""
    odate = str(event.get("original_date", "")).strip()
    otime = str(event.get("original_time", "")).strip()
    if _DATE_RE.match(odate) and otime:
        return odate, otime
    date_s, time_s, _tz = extract_source_fields(event)
    return date_s, time_s


def _utc_wall_clock_matches_source_naive(
    utc_dt: datetime,
    date_s: str,
    time_s: str,
) -> bool:
    """Частая ошибка Gemini: 18:30 London записали как 18:30Z."""
    time_norm, _ = _parse_time_flexible(time_s)
    if not time_norm:
        return False
    utc_aware = utc_dt.astimezone(timezone.utc)
    if utc_aware.date().isoformat() != date_s:
        return False
    return utc_aware.strftime("%H:%M") == time_norm


def reconcile_utc_datetime(
    event: dict[str, Any],
    *,
    trusted_tz: str,
    source_date: str,
    source_time: str,
) -> datetime | None:
    """
    Выбрать один UTC: source+trusted_tz приоритетнее сомнительного utc_datetime.
    """
    time_norm, _ = _parse_time_flexible(source_time)
    if not time_norm or not _DATE_RE.match(source_date):
        return None

    utc_from_source: datetime | None = None
    try:
        utc_from_source = source_to_utc_datetime(source_date, time_norm, trusted_tz)
    except Exception as e:
        log.error("source_to_utc failed: %s", e)

    utc_raw = str(event.get("utc_datetime", "")).strip()
    utc_from_field: datetime | None = None
    if utc_raw:
        utc_from_field = parse_datetime_iso(utc_raw)
        if utc_from_field is not None:
            utc_from_field = utc_from_field.astimezone(timezone.utc)

    if utc_from_source is None and utc_from_field is None:
        return None

    if utc_from_source is None:
        return utc_from_field

    if utc_from_field is None:
        return utc_from_source

  # Gemini часто пишет локальное время в поле UTC
    if _utc_wall_clock_matches_source_naive(utc_from_field, source_date, time_norm):
        if trusted_tz.upper() not in ("UTC", "GMT", "Z"):
            log.warning(
                "UTC field looks like wall clock (not Zulu): title=%r — use %s %s %s",
                event.get("title"),
                source_date,
                time_norm,
                trusted_tz,
            )
            return utc_from_source

    delta = abs(utc_from_field - utc_from_source)
    if delta > timedelta(hours=1, minutes=30):
        log.warning(
            "UTC MISMATCH: title=%r field=%s source+%s=%s delta=%s — prefer source+trusted_tz",
            event.get("title"),
            utc_from_field.isoformat(),
            trusted_tz,
            utc_from_source.isoformat(),
            delta,
        )
        return utc_from_source

    return utc_from_field


def establish_schedule(
    event: dict[str, Any],
    *,
    phase: str = "establish",
) -> dict[str, Any] | None:
    """
    Единственная точка конвертации в VN. Не вызывать повторно на уже locked событии.
    """
    out = dict(event)
    title = out.get("title")

    if out.get("time_locked") and str(out.get("utc_datetime", "")).strip():
        loc = parse_datetime_iso(str(out.get("local_datetime", "")))
        if loc is not None and loc.tzinfo is not None:
            log_event_debug(out, phase=f"{phase}:already_locked")
            return out

    # API-SPORTS: fixture UTC уже Zulu — не reconcile через Europe/London
    if str(out.get("utc_authority", "")).lower() == "api_sports":
        utc_raw = str(out.get("utc_datetime", "")).strip()
        utc_dt = parse_datetime_iso(utc_raw) if utc_raw else None
        if utc_dt is not None:
            fields = utc_datetime_to_local_fields(utc_dt)
            out.update(fields)
            out["time_locked"] = True
            out["schedule_locked"] = True
            log_event_debug(out, phase=f"{phase}:api_sports_utc")
            return out

    trusted_tz = resolve_trusted_source_timezone(out)
    source_date, source_time = extract_source_wall_clock(out)

    if not trusted_tz:
        log.warning("establish_schedule: no trusted_tz title=%r", title)
        return None

    if source_date and source_time:
        utc_dt = reconcile_utc_datetime(
            out,
            trusted_tz=trusted_tz,
            source_date=source_date,
            source_time=source_time,
        )
    else:
        utc_raw = str(out.get("utc_datetime", "")).strip()
        if not utc_raw:
            return None
        utc_dt = parse_datetime_iso(utc_raw)
        if utc_dt is None:
            return None

    if utc_dt is None:
        return None

    fields = utc_datetime_to_local_fields(utc_dt)
    out.update(fields)
    out["original_date"] = source_date or out.get("original_date", "")
    out["original_time"] = source_time or out.get("original_time", "")
    out["original_timezone"] = trusted_tz
    out["source_timezone"] = trusted_tz

    prec = str(out.get("time_precision", "exact")).lower()
    tm = out["local_time"]
    out["time_display"] = f"≈{tm}" if prec == "estimated" else tm
    out["display_time"] = tm
    out["time_locked"] = True
    out["schedule_locked"] = True

    log_event_debug(out, phase=phase)
    return out


def log_event_debug(event: dict[str, Any], *, phase: str = "") -> None:
    prefix = f"[{phase}] " if phase else ""
    log.info("%sEVENT DEBUG:", prefix)
    log.info("%s  title=%s", prefix, event.get("title"))
    log.info(
        "%s  source_time=%s %s",
        prefix,
        event.get("original_date") or event.get("date"),
        event.get("original_time") or event.get("time"),
    )
    log.info(
        "%s  source_timezone=%s",
        prefix,
        event.get("source_timezone") or event.get("original_timezone"),
    )
    log.info("%s  utc_time=%s", prefix, event.get("utc_datetime"))
    log.info(
        "%s  asia_hcm_time=%s %s",
        prefix,
        event.get("local_weekday") or event.get("weekday"),
        event.get("local_time") or event.get("time"),
    )
    log.info(
        "%s  vn_time=%s %s",
        prefix,
        event.get("local_weekday") or event.get("weekday"),
        event.get("local_time") or event.get("time"),
    )
    log.info("%s  timezone_applied=%s", prefix, TARGET_TZ_NAME)
